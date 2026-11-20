#!/bin/bash
# Builds Lastbook.app for Apple Silicon. Needs uv. Run from anywhere:
#   bash packaging/build_app_macos.sh
#
# Each build gets a new folder, derived/app-build/mac/<date-time>/, so nothing ever has to be removed.
# derived/app-build/mac/Lastbook.app points at the newest good one.
# The app is ad-hoc signed by the build tool and nothing more: no certificate, no notarization.
# Optional: put one word per line in derived/app-build/scan-terms.txt (machine names, user names) and the scan refuses a package holding any.
set -euo pipefail
HERE="$(dirname "$(/bin/realpath "$0")")"
ROOT="$(dirname "$HERE")"
BUILD="$ROOT/derived/app-build"
PYVER=3.11.18   # 3.11.18 or later: earlier 3.11 releases lack the block on the compromised eMudhra CA keys (security audit, 12 Nov 2026)
VENV="$BUILD/mac-venv-$PYVER"
OUT="$BUILD/mac/$(date +%Y%m%d-%H%M%S)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$BUILD/mac/pycache"
export PYINSTALLER_CONFIG_DIR="$BUILD/mac/pyinstaller-cache"
if [ "$(uname -m)" != arm64 ]; then
  echo "This build is for Apple Silicon." >&2
  exit 1
fi
mkdir -p "$OUT"
[ -x "$VENV/bin/python" ] || uv venv --python "$PYVER" "$VENV"
"$VENV/bin/python" -c "import sys; v = '.'.join(map(str, sys.version_info[:3])); sys.exit(None if v == '$PYVER' else f'build Python is {v}, not $PYVER')"
uv pip install --python "$VENV/bin/python" -r "$HERE/requirements_macos.txt"
"$VENV/bin/python" -I -B "$HERE/ship_app_files.py" "$OUT/ship"   # web/ and content/ as they ship: see ship_edits.json
# What setup can install (docs/SETUP-API.md): the packs publish-packs has verified, with Small from the 1.0.0 release, and the model.
# A fork's build (GitHub Actions) brings its own instead: LASTBOOK_MANIFEST, from packaging/fork_manifest.py.
if [ -n "${LASTBOOK_MANIFEST:-}" ]; then
  mkdir -p "$OUT/ship/setup" && cp "$LASTBOOK_MANIFEST" "$OUT/ship/setup/manifest-v1.json"
else
  "$VENV/bin/python" -I -B "$HERE/release_manifest.py" --small-from "$ROOT/derived/release/1.0.0-20261104-1507" --out "$OUT/ship/setup/manifest-v1.json"
fi
# Release gate: setup's downloader verifies every download host through engine/setup/roots.pem only (no system trust store)
"$VENV/bin/python" -I -B "$HERE/check_tls_roots.py" || { echo "TLS roots gate failed: refresh engine/setup/roots.pem (packaging/setup_roots.py)" >&2; exit 1; }
export LASTBOOK_SHIP="$OUT/ship"
"$VENV/bin/python" -m PyInstaller --noconfirm --distpath "$OUT/dist" --workpath "$OUT/work" "$HERE/lastbook_macos.spec" 2>&1 | tee "$OUT/build.log"
NAME="$("$VENV/bin/python" -I -B -c "import sys; sys.path.insert(0, '$ROOT'); from engine import distribution; print(distribution.APP_NAME)")"
BUNDLE="$OUT/dist/$NAME.app"
"$VENV/bin/python" "$HERE/scan_package_macos.py" "$BUNDLE" --terms-file "$BUILD/scan-terms.txt" | tee "$OUT/scan.txt"
codesign --verify --deep --strict "$BUNDLE"
ln -sfn "$BUNDLE" "$BUILD/mac/Lastbook.app"
echo "$BUNDLE" > "$BUILD/mac/latest-bundle.txt"
du -sh "$BUNDLE"
echo "Built: $BUNDLE"
