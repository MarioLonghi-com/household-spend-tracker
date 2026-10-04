"""Household colours, and the promise that none of them can be unusable.

The feature's whole claim is that a household cannot make its own ledger hard to
read. That claim rests on two things, and both are tested here: the shipped
palettes pass the same contrast rules the app enforces on the user, and the one
value a household can choose is checked before it is stored.
"""

from __future__ import annotations

import pytest

from app import theming
from app.errors import ValidationError

#: Every text-on-background pair the stylesheet actually produces. A palette is
#: only as good as its worst pairing, so they are all named rather than sampled.
TEXT_PAIRS = (
    ("ink", "paper"),
    ("ink", "surface"),
    ("ink", "surface_2"),
    ("muted", "surface"),
    ("muted", "paper"),
    ("muted", "surface_2"),
    ("danger", "surface"),
    ("warn", "surface"),
    ("positive", "surface"),
    ("accent_ink", "accent"),
)


@pytest.mark.parametrize("palette", theming.PALETTES, ids=lambda p: p.key)
@pytest.mark.parametrize("scheme_name", ["light", "dark"])
def test_every_shipped_palette_is_readable(palette: theming.Palette, scheme_name: str):
    """The app refuses an accent below 4.5:1, so its own colours must clear it.

    Shipping a palette that would fail the rule the user is held to is the
    version of this feature that quietly makes the app worse.
    """
    scheme = getattr(palette, scheme_name)
    for front, back in TEXT_PAIRS:
        ratio = theming.contrast_ratio(getattr(scheme, front), getattr(scheme, back))
        assert ratio >= theming.TEXT_CONTRAST, (
            f"{palette.key} {scheme_name}: {front} on {back} is {ratio:.2f}, "
            f"needs {theming.TEXT_CONTRAST}"
        )


@pytest.mark.parametrize("palette", theming.PALETTES, ids=lambda p: p.key)
@pytest.mark.parametrize("scheme_name", ["light", "dark"])
def test_every_accent_is_visible_against_its_own_page(palette, scheme_name: str):
    """It is a button and a focus ring before it is a decoration."""
    scheme = getattr(palette, scheme_name)
    ratio = theming.contrast_ratio(scheme.accent, scheme.surface)
    assert ratio >= theming.SHAPE_CONTRAST, f"{palette.key} {scheme_name}: {ratio:.2f}"


def test_the_palettes_are_actually_different():
    """Six names for one colour would not tell two households apart.

    Compared by hue, not by contrast. Contrast is a luminance ratio and says
    nothing about colour: a green and a blue of the same darkness score about
    1.1 against each other while being obviously different, which is exactly
    what a set of palettes wants. Hue is the thing being claimed here.
    """
    accents = [palette.light.accent for palette in theming.PALETTES]
    assert len(set(accents)) == len(accents)

    for one in theming.PALETTES:
        for other in theming.PALETTES:
            if one.key >= other.key:
                continue
            first = theming._to_hsl(one.light.accent)[0]
            second = theming._to_hsl(other.light.accent)[0]
            apart = abs(first - second)
            apart = min(apart, 360 - apart)  # the hue circle wraps
            assert apart > 20, (
                f"{one.key} and {other.key} are {apart:.0f} degrees apart on the "
                "colour wheel -- too close to tell two households apart"
            )


# --------------------------------------------------------------------------- #
# The contrast maths itself
# --------------------------------------------------------------------------- #


def test_contrast_matches_the_published_figures():
    """Checked against values anyone can verify, not against this code's output.

    Black on white is 21:1 by definition; the other two are the worked examples
    in the WCAG 2.1 understanding documents.
    """
    assert round(theming.contrast_ratio("#000000", "#ffffff"), 2) == 21.0
    assert round(theming.contrast_ratio("#ffffff", "#ffffff"), 2) == 1.0
    assert round(theming.contrast_ratio("#777777", "#ffffff"), 2) == 4.48
    assert round(theming.contrast_ratio("#767676", "#ffffff"), 2) == 4.54


def test_contrast_does_not_care_which_way_round():
    assert theming.contrast_ratio("#15705c", "#ffffff") == theming.contrast_ratio(
        "#ffffff", "#15705c"
    )


@pytest.mark.parametrize("shorthand,full", [("#abc", "#aabbcc"), ("fff", "#ffffff")])
def test_a_colour_can_be_written_the_short_way(shorthand: str, full: str):
    plan = theming.plan_accent(shorthand, theming.BY_KEY["moss"])
    assert plan.chosen == full


@pytest.mark.parametrize("bad", ["", "green", "#12", "#1234567", "#12345g", "rgb(1,2,3)"])
def test_nonsense_is_refused(bad: str):
    with pytest.raises(ValidationError, match="not a colour"):
        theming.plan_accent(bad, theming.BY_KEY["moss"])


# --------------------------------------------------------------------------- #
# What a chosen accent becomes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("palette", theming.PALETTES, ids=lambda p: p.key)
@pytest.mark.parametrize(
    "picked",
    ["#15705c", "#b03030", "#2f5d9e", "#ffff00", "#000000", "#ffffff", "#777777", "#ff5722"],
)
def test_any_colour_a_person_can_pick_ends_up_usable(palette, picked: str):
    """The promise, stated as a test: there is no accent that breaks a theme.

    Not "most colours work" -- every one of these is a colour a person could
    reasonably choose out of a picker, including the three that are hopeless on
    their own (pure black, pure white, pure yellow), and each must come out the
    other side readable in both schemes.
    """
    plan = theming.plan_accent(picked, palette)

    for scheme_name, accent, ink in (
        ("light", plan.light, plan.light_ink),
        ("dark", plan.dark, plan.dark_ink),
    ):
        scheme = getattr(palette, scheme_name)
        assert theming.contrast_ratio(accent, ink) >= theming.TEXT_CONTRAST, (
            f"{picked} in {palette.key} {scheme_name}: nothing readable on it"
        )
        assert theming.contrast_ratio(accent, scheme.surface) >= theming.SHAPE_CONTRAST, (
            f"{picked} in {palette.key} {scheme_name}: invisible against the page"
        )


def test_the_hue_survives_even_when_the_brightness_cannot():
    """Adjusted, not replaced. A red accent must still look red."""
    plan = theming.plan_accent("#ff0000", theming.BY_KEY["moss"])
    for shade in (plan.light, plan.dark):
        red, green, blue = (int(shade[i : i + 2], 16) for i in (1, 3, 5))
        assert red > green and red > blue, f"{shade} is not recognisably red"


def test_a_dark_scheme_gets_a_paler_accent_than_a_light_one():
    """The two schemes need opposite weights, which is why one value is not enough."""
    plan = theming.plan_accent("#2f5d9e", theming.BY_KEY["moss"])
    light = theming._relative_luminance(plan.light)
    dark = theming._relative_luminance(plan.dark)
    assert dark > light, f"dark {plan.dark} is not paler than light {plan.light}"


def test_what_was_picked_is_what_is_stored():
    """The adjustment happens on the way out, so the household's choice is kept.

    If the derived shade were stored instead, editing the accent twice would
    walk it away from the colour that was chosen.
    """
    plan = theming.plan_accent("#FF5722", theming.BY_KEY["moss"])
    assert plan.chosen == "#ff5722"


# --------------------------------------------------------------------------- #
# Picking one for a new household
# --------------------------------------------------------------------------- #


def test_a_new_household_does_not_repeat_a_colour_in_use():
    assert theming.next_palette([]) == "moss"
    assert theming.next_palette(["moss"]) == "slate"
    assert theming.next_palette(["moss", "slate"]) == "clay"
    assert theming.next_palette(["slate", "clay"]) == "moss"


def test_past_the_end_of_the_table_it_reuses_the_least_used():
    everything = [palette.key for palette in theming.PALETTES]
    # Every palette used once, and moss twice: moss is the one to avoid.
    assert theming.next_palette([*everything, "moss"]) != "moss"
    # All equal again, so any is fair -- but it must still be a real one.
    assert theming.next_palette(everything) in theming.BY_KEY


# --------------------------------------------------------------------------- #
# Resolving
# --------------------------------------------------------------------------- #


def test_without_an_accent_a_household_wears_the_palette_unchanged():
    light, dark = theming.resolve("slate", None)
    assert light == theming.BY_KEY["slate"].light
    assert dark == theming.BY_KEY["slate"].dark


def test_with_an_accent_only_the_accent_moves():
    plain_light, _ = theming.resolve("slate", None)
    light, _ = theming.resolve("slate", "#b03030")
    assert light.accent != plain_light.accent
    assert light.ink == plain_light.ink, "the text colour is not the household's to change"
    assert light.paper == plain_light.paper
    assert light.surface == plain_light.surface


def test_an_unknown_palette_still_opens_the_household():
    """Losing your colour is a nuisance. Losing your ledger is not."""
    light, _ = theming.resolve("a palette that was retired", None)
    assert light == theming.PALETTES[0].light


def test_the_label_colour_is_chosen_to_suit_the_accent():
    """Dark text on a pale accent, light text on a deep one.

    Written because the obvious shortcut -- always white, because most accents
    are deep -- passes every contrast assertion above. The search that picks a
    shade uses `ink_for`, so hard-coding white simply pushes every accent darker
    until white works: nothing becomes unreadable, and a pale yellow silently
    turns olive. Readable is not the same as right.
    """
    moss = theming.BY_KEY["moss"]

    # Yellow can only be pale, so its label has to be dark.
    yellow = theming.plan_accent("#ffff00", moss)
    assert theming._relative_luminance(yellow.dark_ink) < 0.1, (
        f"a pale accent {yellow.dark} was given a pale label {yellow.dark_ink}"
    )
    assert yellow.dark.lower().startswith("#ff"), "and it stayed yellow rather than going olive"

    # A deep violet is the other way round.
    violet = theming.plan_accent("#2f1050", moss)
    assert theming._relative_luminance(violet.light_ink) > 0.5, (
        f"a deep accent {violet.light} was given a dark label {violet.light_ink}"
    )


def test_ink_for_picks_the_better_of_the_two():
    """The unit underneath, stated plainly."""
    moss = theming.BY_KEY["moss"]
    assert theming.ink_for("#111111", moss.light) == "#ffffff"
    assert theming._relative_luminance(theming.ink_for("#eeeeee", moss.light)) < 0.1
