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

# The processing state in an altool answer: `processingState: VALID` or
# `Processing State: VALID` from altool 26, `IMPORT-STATUS: VALID` from the
# older format. Empty when there is none -- still processing, or not an answer.
state_in() {
  printf '%s\n' "$1" |
    sed -n -E "s/^.*(processingState|[Pp]rocessing [Ss]tate|IMPORT-STATUS)[\"']?[:=] *[\"']?([A-Z_]+).*$/\2/p" |
    head -1
}

# `--build-status` arrived in altool 26; Xcode 16's prints its usage instead,
# and the loop below would wait out its patience on that. Say so before the
# upload, while a red job still means nothing went to Apple.
has_build_status() {
  xcrun altool --help 2>&1 | grep -q -- "--build-status"
}

wait_for() {
  tries="${ASC_WAIT_TRIES:-30}"
  while [ "$tries" -gt 0 ]; do
    # altool answers non-zero while a delivery is seconds old and App Store
    # Connect does not know it yet -- 0.16.0's first status call, 0.3 s after
    # the upload -- and that is a reason to ask again, not to stop. Under
    # `set -e` the failing substitution ended the script with no message,
    # after an upload Apple went on to accept.
    said="$(asked --build-status --delivery-id "$1")" || true
    # The first answer goes in the log whole: 0.16.0 and 0.16.1 waited 30
    # minutes on answers nobody saw (altool's usage text, as it turned out).
    [ "$tries" = "${ASC_WAIT_TRIES:-30}" ] && printf '%s\n' "$said"
    state="$(state_in "$said")"
    case "$state" in
      VALID | READY | COMPLETE)
        echo "delivery $1: $state, on App Store Connect"
        return 0
        ;;
      FAILED | INVALID)
        printf '%s\n' "$said" >&2
        echo "delivery $1: Apple refused the build while processing it ($state)" >&2
        return 1
        ;;
    esac
    tries=$((tries - 1))
    [ "$tries" -gt 0 ] && sleep "${ASC_WAIT_SECONDS:-60}"
  done
  printf '%s\n' "$said" | tail -5 >&2
  echo "delivery $1: still being processed; its verdict will be in App Store Connect" >&2
  return 1
}

has_build_status || {
  echo "this altool ($(xcrun --find altool 2>/dev/null || echo 'not found')) has no --build-status; use Xcode 26 or later (DEVELOPER_DIR)" >&2
  exit 2
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
