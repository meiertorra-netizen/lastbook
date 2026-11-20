#!/bin/bash
# Signs, packs and notarizes a built app for download: APP.app -> NAME-VERSION-mac.dmg. Used by .github/workflows/app.yml.
#
#   bash packaging/sign_macos.sh "path/to/Name.app" OUT_DIR VERSION
#
# With MACOS_SIGN_IDENTITY set ("Developer ID Application: Your Name (TEAMID)", in a keychain this shell can use), every
# Mach-O file in the bundle is signed inside out, then the bundle, with the hardened runtime and packaging/entitlements.plist,
# and the .dmg is signed too. With NOTARY_KEY (path to an App Store Connect API key, .p8), NOTARY_KEY_ID and NOTARY_ISSUER
# also set, the .dmg is notarized (xcrun notarytool, waiting for Apple's answer) and the ticket stapled to it, so it opens
# without a warning on a computer that is offline. Without MACOS_SIGN_IDENTITY the app keeps its ad-hoc signature and the
# .dmg is unsigned: macOS then asks people to allow it in System Settings, Privacy & Security.
set -euo pipefail
APP="$1"; OUT="$2"; VERSION="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="$(basename "$APP" .app)"
DMG="$OUT/${NAME// /-}-$VERSION-mac.dmg"
mkdir -p "$OUT"

if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  # Inside out: libraries and helper programs first (lastbook-llm, the Python extensions, the dylibs), the bundle last.
  find "$APP/Contents" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0 |
    while IFS= read -r -d '' f; do
      if file "$f" | grep -q 'Mach-O'; then
        codesign --force --timestamp --options runtime --entitlements "$HERE/entitlements.plist" --sign "$MACOS_SIGN_IDENTITY" "$f"
      fi
    done
  codesign --force --timestamp --options runtime --entitlements "$HERE/entitlements.plist" --sign "$MACOS_SIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
fi

# The disk image: the app and a link to /Applications to drag it onto.
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG"
rm -rf "$STAGE"

if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$DMG"
  if [ -n "${NOTARY_KEY:-}" ] && [ -n "${NOTARY_KEY_ID:-}" ] && [ -n "${NOTARY_ISSUER:-}" ]; then
    xcrun notarytool submit "$DMG" --key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER" --wait --timeout 2h
    xcrun stapler staple "$DMG"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
  else
    echo "signed but not notarized: NOTARY_KEY, NOTARY_KEY_ID and NOTARY_ISSUER are not all set" >&2
  fi
else
  echo "unsigned: MACOS_SIGN_IDENTITY is not set" >&2
fi
shasum -a 256 "$DMG"
echo "$DMG"
