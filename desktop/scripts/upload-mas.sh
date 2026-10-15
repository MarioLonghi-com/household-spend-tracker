#!/bin/sh
# Upload a Mac App Store package and wait for Apple's verdict on it (#351).
#
#   ASC_KEY_ID=... ASC_ISSUER_ID=... scripts/upload-mas.sh OUT.pkg
#   ASC_KEY_ID=... ASC_ISSUER_ID=... scripts/upload-mas.sh --status DELIVERY_ID
#
# The package is `package-mas.sh`'s. altool finds the App Store Connect API
# key as ~/private_keys/AuthKey_$ASC_KEY_ID.p8; the key never goes into the
# repository.
#
# "UPLOAD SUCCEEDED" says the bytes arrived, not that the build will be used:
# Apple processes it afterwards and can still refuse it -- 0.12.0's first
# upload was, for a quarantine attribute (ITMS 91109), after altool had said
# it succeeded. So this validates first, uploads, and then asks for the
# delivery's import status until it is VALID (exit 0) or refused (exit 1).
# ASC_WAIT_TRIES and ASC_WAIT_SECONDS set the patience: 30 and 60 by default.
set -eu
: "${ASC_KEY_ID:?the App Store Connect API key id}"
: "${ASC_ISSUER_ID:?the App Store Connect API issuer id}"

asked() {
  xcrun altool "$@" --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID" 2>&1
}

wait_for() {
  tries="${ASC_WAIT_TRIES:-30}"
  while [ "$tries" -gt 0 ]; do
    said="$(asked --build-status --delivery-id "$1")"
    import="$(printf '%s\n' "$said" | sed -n 's/^.*IMPORT-STATUS: *//p' | head -1 | tr -d ' \r')"
    case "$import" in
      VALID)
        printf '%s\n' "$said" | grep -E "BUILD-STATUS|IMPORT-STATUS|IS-ON-APP-STORE-CONNECT" || true
        echo "delivery $1: VALID, on App Store Connect"
        return 0
        ;;
      FAILED | INVALID)
        printf '%s\n' "$said" | sed -n '/PROCESSING-ERRORS/,/IMPORT-STATUS/p' >&2
        echo "delivery $1: Apple refused the build while processing it" >&2
        return 1
        ;;
    esac
    tries=$((tries - 1))
    [ "$tries" -gt 0 ] && sleep "${ASC_WAIT_SECONDS:-60}"
  done
  echo "delivery $1: still being processed; its verdict will be in App Store Connect" >&2
  return 1
}

if [ "${1:-}" = "--status" ]; then
  wait_for "${2:?a delivery id}"
  exit $?
fi

PKG="${1:?the package to upload}"
[ -f "$PKG" ] || { echo "no package at $PKG" >&2; exit 2; }

checked="$(asked --validate-app -f "$PKG" -t macos)"
printf '%s\n' "$checked" | grep -E "SUCCEEDED|FAILED|ERROR" || true
printf '%s\n' "$checked" | grep -q "VERIFY SUCCEEDED" || { echo "validation refused $PKG" >&2; exit 1; }

sent="$(asked --upload-app -f "$PKG" -t macos)"
printf '%s\n' "$sent" | grep -E "SUCCEEDED|FAILED|Delivery UUID" || true
printf '%s\n' "$sent" | grep -q "UPLOAD SUCCEEDED" || { echo "the upload of $PKG failed" >&2; exit 1; }
delivery="$(printf '%s\n' "$sent" | sed -n 's/^.*Delivery UUID: *//p' | head -1 | tr -d ' \r')"
[ -n "$delivery" ] || { echo "altool named no delivery id" >&2; exit 1; }
wait_for "$delivery"
