"""Assemble a *Complete Station Bundle* — a single downloadable .zip that is the
runnable offline station with its scope package baked in.

Layout produced (extracts to one folder the operator double-clicks into):

    lazeims-station-<CODE>/
      start.sh            one-click launcher (Linux/macOS)
      start.command       macOS Finder double-click shim -> start.sh
      start.bat           one-click launcher (Windows)
      README-FIRST.txt    3-line instructions
      pyproject.toml      station project (vendored)
      station/            the FastAPI app (vendored)
      vendor/lazeims-common/   shared contract package (vendored, no sibling needed)
      wheelhouse/         optional pinned wheels for fully-offline first run
      station_data/
        import/<package_id>.json   the signed {manifest, seed} — auto-imported on boot

Running ``start.sh`` / ``start.bat`` makes a local ``.venv``, installs the two
vendored packages (offline from ``wheelhouse/`` when present), migrates SQLite,
auto-imports the bundled package, and serves the station on ``0.0.0.0:8080``.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from ..config import get_settings

# Directory / file names never worth shipping.
_EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules", "station_data"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".sqlite3"}
_EXCLUDE_NAMES = {".session_secret", ".deps_installed", ".DS_Store"}


def _central_root() -> Path:
    # app/services/station_bundle.py -> parents[2] == central-api repo root
    return Path(__file__).resolve().parents[2]


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (_central_root() / p).resolve()


def _should_skip(rel_parts: tuple[str, ...], name: str) -> bool:
    if any(part in _EXCLUDE_DIRS for part in rel_parts):
        return True
    if name in _EXCLUDE_NAMES:
        return True
    if any(name.endswith(sfx) for sfx in _EXCLUDE_SUFFIXES):
        return True
    if name.endswith(".egg-info") or any(part.endswith(".egg-info") for part in rel_parts):
        return True
    return False


def _add_tree(zf: zipfile.ZipFile, src_dir: Path, arc_prefix: str) -> None:
    """Recursively add ``src_dir`` into the zip under ``arc_prefix/``, preserving
    file permissions (so a bundled runtime's ``bin/python3`` stays executable)."""
    import os as _os

    src_dir = src_dir.resolve()
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_dir)
        if _should_skip(rel.parts[:-1], path.name):
            continue
        zi = zipfile.ZipInfo(f"{arc_prefix}/{rel.as_posix()}")
        zi.external_attr = (_os.stat(path).st_mode & 0xFFFF) << 16
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, path.read_bytes())


# ── launcher scripts (bundle-specific: no sibling-repo assumption) ───────────

_START_SH = """\
#!/usr/bin/env bash
# LAZEIMS Station — one-click launcher (Linux/macOS) with full logging.
# Uses ONE shared environment per computer (not one per download) and runs the
# station straight from this bundle. Everything is printed AND written to
# ./launch.log; the window stays open on any error so you can read it.
cd "$(dirname "$0")"
BUNDLE="$(pwd)"
LOG="./launch.log"
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
hold() { echo ""; read -r -p "Press Enter to close this window..." _ || true; }
trap 'c=$?; if [ $c -ne 0 ]; then echo ""; log "Launcher stopped (exit $c). See messages above / launch.log."; hold; fi' EXIT

log "== LAZEIMS Station =="
log "Bundle : $BUNDLE"
PORT="${STATION_PORT:-8080}"

# ONE shared environment for this computer, reused by every station bundle.
ENV_DIR="${STATION_ENV_DIR:-$HOME/.lazeims-station/env}"
log "Shared env : $ENV_DIR"

# 1) find Python 3.11+ (prefer a bundled zero-install runtime if shipped)
PY=""
if [ -x "./runtime/bin/python3" ]; then PY="./runtime/bin/python3"; fi
for cand in "$PY" "${PYTHON:-}" python3 python; do
  [ -n "$cand" ] || continue
  command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
done
if [ -z "$PY" ]; then
  log "ERROR: Python 3.11+ is not installed on this computer."
  log "Fix: install Python from https://www.python.org/downloads/ then double-click again."
  exit 1
fi
log "Python : $("$PY" --version 2>&1)  ($(command -v "$PY"))"

# 2) shared environment — created only the first time on this computer
if [ ! -x "$ENV_DIR/bin/python" ]; then
  log "Creating the shared environment (first time on this computer only)..."
  mkdir -p "$(dirname "$ENV_DIR")"
  if ! "$PY" -m venv "$ENV_DIR"; then
    log "ERROR: could not create the environment (is the 'venv' module available?)."
    exit 1
  fi
fi
VPY="$ENV_DIR/bin/python"

# 3) shared dependencies — installed once, then reused by all bundles
if [ ! -f "$ENV_DIR/.deps_ok" ]; then
  if [ -d wheelhouse ]; then
    log "Installing shared dependencies from bundled wheelhouse (offline)..."
    if ! "$VPY" -m pip install --no-index --find-links wheelhouse \\
         fastapi "uvicorn[standard]" argon2-cffi itsdangerous python-multipart pydantic cryptography; then
      log "ERROR: dependency install failed (see above / launch.log)."; exit 1
    fi
  else
    log "Installing shared dependencies from the internet (first time only)..."
    if ! "$VPY" -m pip install \\
         fastapi "uvicorn[standard]" argon2-cffi itsdangerous python-multipart pydantic cryptography; then
      log "ERROR: dependency install failed (see above / launch.log)."; exit 1
    fi
  fi
  : > "$ENV_DIR/.deps_ok"
else
  log "Shared dependencies already present — reusing them (no install needed)."
fi

# 4) run this bundle's station from source; data + package stay in this folder
export PYTHONPATH="$BUNDLE:$BUNDLE/vendor/lazeims-common${PYTHONPATH:+:$PYTHONPATH}"
export STATION_DATA_DIR="$BUNDLE/station_data"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${IP:-}" ] && IP="127.0.0.1"
log "Starting the station on port $PORT ..."
log "  Open on this device : http://127.0.0.1:$PORT"
log "  Open on the LAN     : http://$IP:$PORT"
log "Leave this window OPEN while marking. Close it to stop the station."
"$VPY" -m uvicorn station.main:app --host 0.0.0.0 --port "$PORT"
"""

_START_COMMAND = """\
#!/usr/bin/env bash
# macOS Finder double-click shim -> runs start.sh (which logs + holds open).
cd "$(dirname "$0")"
bash ./start.sh
"""

_START_BAT = """\
@echo off
REM LAZEIMS Station - one-click launcher (Windows) with logging + keep-open.
REM Uses ONE shared environment per computer (not one per download).
setlocal enabledelayedexpansion
cd /d "%~dp0"
set BUNDLE=%CD%
set PORT=8080
if not "%STATION_PORT%"=="" set PORT=%STATION_PORT%
set LOG=launch.log
break > "%LOG%"

set ENV_DIR=%STATION_ENV_DIR%
if "%ENV_DIR%"=="" set ENV_DIR=%LOCALAPPDATA%\\lazeims-station\\env

call :log "== LAZEIMS Station =="
call :log "Bundle : %BUNDLE%"
call :log "Shared env : %ENV_DIR%"

REM 1) find Python (prefer a bundled zero-install runtime, then system Python)
set PY=
if exist "runtime\\python.exe" set PY=runtime\\python.exe
for %%P in (py.exe python.exe) do (
  if "!PY!"=="" ( where %%P >nul 2>nul && set PY=%%P )
)
if "!PY!"=="" (
  call :log "ERROR: Python 3.11+ is not installed on this computer."
  call :log "Fix: install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then double-click again."
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%v in ('!PY! --version 2^>^&1') do call :log "Python : %%v"

REM 2) shared environment — created only the first time on this computer
if not exist "%ENV_DIR%\\Scripts\\python.exe" (
  call :log "Creating the shared environment (first time on this computer only)..."
  if not exist "%LOCALAPPDATA%\\lazeims-station" mkdir "%LOCALAPPDATA%\\lazeims-station"
  !PY! -m venv "%ENV_DIR%" >> "%LOG%" 2>&1
  if errorlevel 1 ( call :log "ERROR: venv creation failed. See launch.log." & echo. & pause & exit /b 1 )
)
set VPY=%ENV_DIR%\\Scripts\\python.exe

REM 3) shared dependencies — installed once, then reused by all bundles
if not exist "%ENV_DIR%\\.deps_ok" (
  if exist "wheelhouse" (
    call :log "Installing shared dependencies from bundled wheelhouse (offline)..."
    "%VPY%" -m pip install --no-index --find-links wheelhouse fastapi uvicorn[standard] argon2-cffi itsdangerous python-multipart pydantic cryptography >> "%LOG%" 2>&1
  ) else (
    call :log "Installing shared dependencies from the internet (first time only)..."
    "%VPY%" -m pip install fastapi uvicorn[standard] argon2-cffi itsdangerous python-multipart pydantic cryptography >> "%LOG%" 2>&1
  )
  if errorlevel 1 ( call :log "ERROR: dependency install failed. See launch.log." & echo. & pause & exit /b 1 )
  break > "%ENV_DIR%\\.deps_ok"
) else (
  call :log "Shared dependencies already present - reusing them (no install needed)."
)

REM 4) run this bundle's station from source; data + package stay in this folder
set PYTHONPATH=%BUNDLE%;%BUNDLE%\\vendor\\lazeims-common
set STATION_DATA_DIR=%BUNDLE%\\station_data
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
  if "!IP!"=="" set IP=%%a
)
set IP=!IP: =!
if "!IP!"=="" set IP=127.0.0.1
call :log "Starting the station on port %PORT% ..."
call :log "  Open on this device : http://127.0.0.1:%PORT%"
call :log "  Open on the LAN     : http://!IP!:%PORT%"
call :log "Leave this window OPEN while marking. Close it to stop the station."
"%VPY%" -m uvicorn station.main:app --host 0.0.0.0 --port %PORT%
call :log "Station stopped."
echo.
pause
exit /b 0

:log
echo %~1
echo [%date% %time%] %~1 >> "%LOG%"
goto :eof
"""


def _readme(station_code: str, exam_id: str, package_id: str) -> str:
    return (
        "LAZEIMS Station — Complete Bundle\n"
        "=================================\n\n"
        f"Station : {station_code}\n"
        f"Exam    : {exam_id}\n"
        f"Package : {package_id}\n\n"
        "TO RUN\n"
        "------\n"
        "  Windows : double-click  start.bat\n"
        "  macOS   : double-click  start.command\n"
        "  Linux   : run           ./start.sh\n\n"
        "First run needs Python 3.11+ (from python.org). It sets up ONE shared\n"
        "environment for this computer (reused by every station bundle — it is\n"
        "NOT recreated per download), loads the enclosed exam package\n"
        "automatically, and opens the station at http://<this-computer-ip>:8080\n"
        "on your LAN.\n\n"
        "WHAT GETS WRITTEN\n"
        "-----------------\n"
        "  launch.log            <- everything the launcher did (read this if the\n"
        "                           window closes or something looks wrong)\n"
        "  station_data/         <- this station's database + imported package\n"
        "  the shared environment lives outside the folder:\n"
        "     Windows: %LOCALAPPDATA%\\lazeims-station\\env\n"
        "     macOS/Linux: ~/.lazeims-station/env\n\n"
        "TROUBLESHOOTING\n"
        "---------------\n"
        "  * Window flashed and closed? Open launch.log — the most common cause is\n"
        "    'Python is not installed'. Install Python 3.11+ and run again.\n"
        "  * Move the whole folder as-is; do not run it from inside the .zip.\n\n"
        "Data Enterers sign in with their PIN + initials; the Station Admin signs\n"
        "in with the username + password issued with this package. No internet is\n"
        "needed to enter marks. When back online, marks sync to Central.\n"
    )


def build_bundle_zip(*, station_code: str, exam_id: str, package_id: str, package_bundle: dict) -> bytes:
    """Return the complete station bundle as zip bytes.

    ``package_bundle`` is the stored ``{manifest, seed}`` (what /download returns).
    """
    settings = get_settings()
    station_dir = _resolve(settings.station_app_dir)
    common_dir = _resolve(settings.lazeims_common_dir)
    if not (station_dir / "station").is_dir():
        raise FileNotFoundError(f"station app not found at {station_dir} (set STATION_APP_DIR)")
    if not (common_dir / "lazeims_common").is_dir():
        raise FileNotFoundError(f"lazeims-common not found at {common_dir} (set LAZEIMS_COMMON_DIR)")

    root = f"lazeims-station-{station_code}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # station app: station/ package + project file + readme
        _add_tree(zf, station_dir / "station", f"{root}/station")
        for fname in ("pyproject.toml", "README.md"):
            fp = station_dir / fname
            if fp.is_file():
                zf.writestr(f"{root}/{fname}", fp.read_bytes())

        # vendored shared contract (so no sibling repo is needed)
        _add_tree(zf, common_dir / "lazeims_common", f"{root}/vendor/lazeims-common/lazeims_common")
        common_proj = common_dir / "pyproject.toml"
        if common_proj.is_file():
            zf.writestr(f"{root}/vendor/lazeims-common/pyproject.toml", common_proj.read_bytes())

        # optional offline wheelhouse
        if settings.station_wheelhouse_dir:
            wh = _resolve(settings.station_wheelhouse_dir)
            if wh.is_dir():
                _add_tree(zf, wh, f"{root}/wheelhouse")

        # optional bundled Python runtime (true zero-install)
        if settings.station_runtime_dir:
            rt = _resolve(settings.station_runtime_dir)
            if rt.is_dir():
                _add_tree(zf, rt, f"{root}/runtime")

        # the signed package, placed where the station auto-imports it on boot
        zf.writestr(
            f"{root}/station_data/import/{package_id}.json",
            json.dumps(package_bundle, indent=2).encode("utf-8"),
        )

        # default Central URL for sync-back (the secret sync key is entered by
        # the station admin, never shipped inside the bundle).
        central_url = settings.central_public_base_url.strip()
        if central_url:
            zf.writestr(
                f"{root}/station_data/sync.json",
                json.dumps({"central_url": central_url}, indent=2).encode("utf-8"),
            )

        # launchers + readme
        zi_sh = zipfile.ZipInfo(f"{root}/start.sh"); zi_sh.external_attr = 0o755 << 16
        zf.writestr(zi_sh, _START_SH)
        zi_cmd = zipfile.ZipInfo(f"{root}/start.command"); zi_cmd.external_attr = 0o755 << 16
        zf.writestr(zi_cmd, _START_COMMAND)
        zf.writestr(f"{root}/start.bat", _START_BAT)
        zf.writestr(f"{root}/README-FIRST.txt", _readme(station_code, exam_id, package_id))

    return buf.getvalue()
