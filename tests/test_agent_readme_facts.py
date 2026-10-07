"""Facts `agent/README.md` states about the agent API, held to the code (#41).

Each was missing or loose in the README and an agent learned it from a 422 or
from an empty answer. A number written in prose drifts from the constant it
describes; these fail when it does.
"""

from __future__ import annotations

import pathlib

import pytest

# Reads agent/README.md: run on every pull request, so a README-only change meets it.
pytestmark = pytest.mark.repo_wide

README = (pathlib.Path(__file__).resolve().parent.parent / "agent" / "README.md").read_text()


def test_the_match_window_range_and_default_are_the_codes():
    from app.services.importing import MATCH_WINDOW_DAYS
    from app.services.matching import MAX_WINDOW_DAYS

    said = f"`window_days` is how many days either side of `date` to look: **0 to {MAX_WINDOW_DAYS}**,\ndefault {MATCH_WINDOW_DAYS}."
    assert said in README


def test_the_register_amount_filter_is_said_to_be_unsigned():
    assert "**`amount`\nmatches the figure without its sign**" in README
    assert "matches nothing" in README


def test_the_binary_receipt_door_is_documented_with_its_parameters():
    from app.api.routers.agent import MAX_BASE64_BYTES

    assert '"$H/receipts/binary?filename=' in README
    assert "`filename`, `transaction_id` and `note` ride in the query string" in README
    assert f"The same {MAX_BASE64_BYTES // (1024 * 1024)} MB ceiling" in README
    assert "It takes no `extracted`" in README
