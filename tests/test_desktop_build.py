"""The Microsoft Store's package identity reaches the MSIX through the
environment (#63), never through a commit: electron-builder.yml carries a
test identity, and `scripts.desktop_build` overrides it only when all three
of the Store's values are given."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.desktop_build import APPX_IDENTITY, _appx_identity

STORE = {
    "SPENDTRACKER_APPX_IDENTITY_NAME": "Publisher.HouseholdSpendTracker",
    "SPENDTRACKER_APPX_PUBLISHER": "CN=00000000-0000-0000-0000-000000000000",
    "SPENDTRACKER_APPX_PUBLISHER_DISPLAY_NAME": "Publisher",
}


def test_all_three_become_electron_builder_overrides() -> None:
    assert _appx_identity("win", STORE) == [
        "-c.appx.identityName=Publisher.HouseholdSpendTracker",
        "-c.appx.publisher=CN=00000000-0000-0000-0000-000000000000",
        "-c.appx.publisherDisplayName=Publisher",
    ]


def test_none_given_keeps_the_test_identity() -> None:
    assert _appx_identity("win", {}) == []
    assert _appx_identity("win", {name: "" for name in APPX_IDENTITY}) == []


@pytest.mark.parametrize("left_out", sorted(APPX_IDENTITY))
def test_two_of_three_is_refused_naming_the_missing_one(left_out: str) -> None:
    given = {name: value for name, value in STORE.items() if name != left_out}
    with pytest.raises(SystemExit, match=f"missing: {left_out}$"):
        _appx_identity("win", given)


def test_only_the_windows_package_reads_them() -> None:
    for target in ("mac", "mas", "store"):
        assert _appx_identity(target, STORE) == []


def test_the_mac_build_declares_exempt_encryption() -> None:
    """Export compliance is answered in the app, not by hand for every build.

    Without `ITSAppUsesNonExemptEncryption`, App Store Connect holds each build
    until someone answers the encryption question, and the release job's status
    wait holds with it (v0.17.0). The answer is "exempt" (decided 2026-11-13):
    HTTPS, the OS's own, and standard algorithms protecting the person's own
    data. The `mas` target inherits `mac`'s extendInfo, so it must not set its
    own, and package-mas.sh checks the packaged Info.plist for it.
    """
    import yaml

    root = Path(__file__).resolve().parent.parent / "desktop"
    config = yaml.safe_load((root / "electron-builder.yml").read_text(encoding="utf-8"))
    assert config["mac"]["extendInfo"]["ITSAppUsesNonExemptEncryption"] is False
    assert "extendInfo" not in config.get("mas", {})
    script = (root / "scripts" / "package-mas.sh").read_text(encoding="utf-8")
    assert "Print :ITSAppUsesNonExemptEncryption" in script
