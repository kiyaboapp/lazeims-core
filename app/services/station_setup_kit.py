"""Build the one-time Station Setup Kit.

Chief IT downloads this ZIP once per marking-centre computer. On first
double-click it prepares Python + dependencies (online) and starts the local
LAN server; subsequent starts are immediate and offline. The exam package
itself is a separate small download, imported through the local admin UI.

Contents produced::

    lazeims-station-setup/
      Setup LAZEIMS Station.bat     (double-click)
      Start LAZEIMS Station.bat     (double-click)
      launcher/setup.ps1
      launcher/setup.sh
      launcher/start.ps1
      launcher/start.sh
      station/                      the FastAPI app source
      vendor/lazeims-common/        shared contracts
      requirements.lock             pinned dependencies with hashes
      lazeims-public-key.pem        Ed25519 verification key
      README-FIRST.txt

Windows launchers use PowerShell for colorful output; the ``.bat`` files are
one-line shims so the operator only ever double-clicks a friendly filename.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from lazeims_common.signing import get_public_key_pem

from ..config import get_settings


# Directory / file names never worth shipping.
_EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules", "station_data"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".sqlite3"}
_EXCLUDE_NAMES = {".session_secret", ".session-secret", ".deps_installed", ".DS_Store"}


def _central_root() -> Path:
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
        data = path.read_bytes()
        # PowerShell on Windows reads files as Windows-1252 unless a BOM is
        # present. Re-encode .ps1 files as UTF-8-with-BOM so Unicode characters
        # (em dash, check mark, box-drawing) are parsed correctly.
        if path.suffix.lower() == ".ps1" and not data.startswith(b"\xef\xbb\xbf"):
            try:
                data = b"\xef\xbb\xbf" + data.decode("utf-8").encode("utf-8")
            except UnicodeDecodeError:
                pass  # already non-UTF-8, leave as-is
        zf.writestr(zi, data)


_SETUP_BAT = """@echo off
REM LAZEIMS Station - first-time setup and start.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\\station.ps1"
if errorlevel 1 (
    echo.
    echo Something went wrong. Read the message above, then press any key to close.
    pause > NUL
)
endlocal
"""

_START_BAT = """@echo off
REM LAZEIMS Station - daily launcher.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\\station.ps1"
if errorlevel 1 (
    echo.
    echo The Station could not start. Read the message above, then press any key to close.
    pause > NUL
)
endlocal
"""


_README = """LAZEIMS Offline Station
=======================

Double-click  LAZEIMS Station.bat  to set up and start.

First run: installs everything (needs internet).
Every run after: starts immediately, no reinstall.

Data Enterers connect from any device on the same
network using the address shown in the console.
"""


def build_setup_kit_zip(include_wheelhouse: bool = False) -> bytes:
    """Return the Setup Kit ZIP bytes.

    Args:
        include_wheelhouse: When True, bundle the pre-built Windows wheels for
            fully-offline installs. Default False — the setup script installs
            packages from the internet which is faster and more reliable.
    """
    settings = get_settings()
    station_dir = _resolve(settings.station_app_dir)
    common_dir = _resolve(settings.lazeims_common_dir)
    launcher_dir = station_dir / "launcher"
    if not (station_dir / "station").is_dir():
        raise FileNotFoundError(f"Station app not found at {station_dir}")
    if not (common_dir / "lazeims_common").is_dir():
        raise FileNotFoundError(f"lazeims-common not found at {common_dir}")

    # Only include wheelhouse when explicitly requested
    wheelhouse_dir: Path | None = None
    if include_wheelhouse and settings.station_wheelhouse_dir:
        wh = _resolve(settings.station_wheelhouse_dir)
        if wh.is_dir() and any(wh.iterdir()):
            wheelhouse_dir = wh

    root = "lazeims-station-setup"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Station app
        _add_tree(zf, station_dir / "station", f"{root}/station")

        # Vendored shared contracts
        _add_tree(zf, common_dir / "lazeims_common", f"{root}/vendor/lazeims-common/lazeims_common")

        # Launcher scripts
        if launcher_dir.is_dir():
            _add_tree(zf, launcher_dir, f"{root}/launcher")

        # Pinned dependency lock, if present in the station repo
        for filename in ("requirements.lock", "pyproject.toml"):
            fp = station_dir / filename
            if fp.is_file():
                zf.writestr(f"{root}/{filename}", fp.read_bytes())

        # Pre-built wheelhouse — bundled when STATION_WHEELHOUSE_DIR is configured
        # and the directory is non-empty. The launcher scripts detect its presence
        # and pass --find-links=wheelhouse --no-index to pip so the first run is
        # fully offline.
        if wheelhouse_dir is not None:
            _add_tree(zf, wheelhouse_dir, f"{root}/wheelhouse")

        # Central verification public key
        zf.writestr(f"{root}/lazeims-public-key.pem", get_public_key_pem())

        # Double-click shim and readme — one BAT, one purpose
        zf.writestr(f"{root}/LAZEIMS Station.bat", _SETUP_BAT.replace("\n", "\r\n"))
        zf.writestr(f"{root}/README-FIRST.txt", _README)

    return buf.getvalue()
