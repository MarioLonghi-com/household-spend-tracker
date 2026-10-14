#!/bin/sh
# Sign the Mac App Store app for a sandboxed run on this Mac (#59, #61).
#
#   scripts/sign-mas.sh "dist/mas-dev-arm64/Household Spend Tracker.app" PROFILE.provisionprofile ["Apple Development"]
#
# electron-builder makes the app unsigned (`npm run dist:mas`); this does what
# the spike found a sandboxed Electron app with a frozen Python child needs:
#
# 1. the provisioning profile, embedded. It must name this Mac's
#    *Provisioning UDID* (System Information > Hardware), not its Hardware
#    UUID, or the app hangs in sandbox initialisation;
# 2. the team, read from the profile, as `ElectronTeamID` and in the
#    entitlements' app group -- without the group Electron's Mach port check
#    crashes at launch;
# 3. every Mach-O signed inside-out, each child with `app-sandbox` + `inherit`
#    and the app itself with the full set (entitlements/mas.plist).
#
# The identity defaults to the first "Apple Development" certificate in the
# keychain. Store submission signs with "Apple Distribution" and packages with
# productbuild; that is #63.
set -eu
APP="$1"
PROFILE="$2"
WHO="${3:-Apple Development}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

[ -d "$APP" ] || { echo "no app at $APP" >&2; exit 2; }
[ -f "$PROFILE" ] || { echo "no provisioning profile at $PROFILE" >&2; exit 2; }

ID="$(security find-identity -v -p codesigning | grep "$WHO" | head -1 | awk '{print $2}')"
[ -n "$ID" ] || { echo "no \"$WHO\" signing identity in the keychain" >&2; exit 2; }

PLIST="$(mktemp -t profile)"
security cms -D -i "$PROFILE" > "$PLIST"
TEAM="$(/usr/libexec/PlistBuddy -c 'Print :TeamIdentifier:0' "$PLIST")"
BUNDLE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")"
rm -f "$PLIST"

WORK="$(mktemp -d -t entitlements)"
sed -e "s/@TEAM_ID@/$TEAM/g" -e "s/@BUNDLE_ID@/$BUNDLE/g" "$HERE/entitlements/mas.plist" > "$WORK/parent.plist"
CHILD="$HERE/entitlements/mas.inherit.plist"

cp "$PROFILE" "$APP/Contents/embedded.provisionprofile"
plutil -replace ElectronTeamID -string "$TEAM" "$APP/Contents/Info.plist"

MAIN="$APP/Contents/MacOS/$(basename "$APP" .app)"
# Every Mach-O that is not the main executable, deepest first.
find "$APP/Contents" -type f | while read -r f; do
  if file "$f" | grep -q "Mach-O"; then printf '%s\t%s\n' "$(echo "$f" | tr -cd '/' | wc -c)" "$f"; fi
done | sort -rn | cut -f2- | while read -r f; do
  [ "$f" = "$MAIN" ] && continue
  codesign --force --sign "$ID" --entitlements "$CHILD" "$f" 2>/dev/null
done
# Then the bundles inside it (frameworks, helper apps), deepest first.
find "$APP/Contents" -type d \( -name "*.framework" -o -name "*.app" \) | awk '{ print length($0) "\t" $0 }' | sort -rn | cut -f2- | while read -r b; do
  codesign --force --sign "$ID" --entitlements "$CHILD" "$b"
done
codesign --force --sign "$ID" --entitlements "$WORK/parent.plist" "$APP"
rm -rf "$WORK"
codesign --verify --deep --strict "$APP"
echo "signed for team $TEAM: $APP"
