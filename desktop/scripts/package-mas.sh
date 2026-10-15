#!/bin/sh
# The installer package the Mac App Store takes (#63).
#
#   scripts/package-mas.sh "dist/mas-arm64/Household Spend Tracker.app" OUT.pkg
#
# The app must already be signed for the store: `scripts/sign-mas.sh` with the
# App Store provisioning profile and the "Apple Distribution" identity. This
# wraps it in a package for /Applications, signed with the installer identity
# ("3rd Party Mac Developer Installer", which the developer site now calls
# Mac Installer Distribution), and checks both signatures before saying so.
#
# Upload the result with Transporter, or with altool and an App Store Connect
# API key kept in ~/private_keys -- the key never goes into the repository:
#
#   xcrun altool --upload-app -f OUT.pkg -t macos --apiKey KEY_ID --apiIssuer ISSUER_ID
set -eu
APP="$1"
OUT="$2"
WHO="${3:-3rd Party Mac Developer Installer}"
[ -d "$APP" ] || { echo "no app at $APP" >&2; exit 2; }
# The app's own signature first: a package around an app signed for
# development, or not for this team, is refused at upload, much later.
codesign --verify --deep --strict "$APP"
AUTHORITY="$(codesign -dvv "$APP" 2>&1 | sed -n 's/^Authority=//p' | head -1)"
case "$AUTHORITY" in
  "Apple Distribution:"*) ;;
  *) echo "the app is signed by \"$AUTHORITY\", not an Apple Distribution identity: run sign-mas.sh with the App Store profile first" >&2; exit 2 ;;
esac
[ -f "$APP/Contents/embedded.provisionprofile" ] || { echo "no provisioning profile embedded in $APP" >&2; exit 2; }
# The store refuses an app holding any file marked as downloaded (ITMS 91109),
# and only says so after the upload, while processing it.
if xattr -r "$APP" 2>/dev/null | grep -q "com.apple.quarantine"; then
  echo "files in the app carry com.apple.quarantine, which the store refuses: sign it again with sign-mas.sh" >&2
  xattr -r "$APP" | grep "com.apple.quarantine" | head -3 >&2
  exit 2
fi
ID="$(security find-identity -v | grep "$WHO" | head -1 | awk '{print $2}')"
[ -n "$ID" ] || { echo "no \"$WHO\" identity in the keychain" >&2; exit 2; }
productbuild --component "$APP" /Applications --sign "$ID" "$OUT"
pkgutil --check-signature "$OUT" | sed -n '1,4p'
echo "packaged: $OUT ($(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist"), build $(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist"))"
