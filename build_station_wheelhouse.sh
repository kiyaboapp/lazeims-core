#!/usr/bin/env bash
# Build an OFFLINE wheelhouse of the station runtime dependencies so a Complete
# Station Bundle can install with no internet on the marking PC.
#
# Usage:
#   ./build_station_wheelhouse.sh [DEST_DIR]
# Then point Central at it and restart:
#   export STATION_WHEELHOUSE_DIR="$(pwd)/DEST_DIR"   # (in .env)
#
# Wheels are platform-specific. Build on / for the same OS+arch+Python as the
# marking PCs, or cross-download with pip's --platform/--python-version/
# --only-binary=:all: flags. Example (Windows x64, py3.11):
#   python -m pip download --dest wh_win \
#     --only-binary=:all: --platform win_amd64 --python-version 3.11 \
#     fastapi "uvicorn[standard]" argon2-cffi itsdangerous python-multipart pydantic cryptography
set -euo pipefail
DEST="${1:-station_wheelhouse}"
mkdir -p "$DEST"
python3 -m pip download --dest "$DEST" \
  fastapi "uvicorn[standard]" argon2-cffi itsdangerous python-multipart pydantic cryptography
echo "Wheelhouse built at $DEST ($(ls "$DEST" | wc -l) files)."
echo "Set STATION_WHEELHOUSE_DIR=$(cd "$DEST" && pwd) and restart the API to include it in bundles."
