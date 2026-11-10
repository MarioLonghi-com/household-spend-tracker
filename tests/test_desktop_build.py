"""The Microsoft Store's package identity reaches the MSIX through the
environment (#63), never through a commit: electron-builder.yml carries a
test identity, and `scripts.desktop_build` overrides it only when all three
of the Store's values are given."""

from __future__ import annotations

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
