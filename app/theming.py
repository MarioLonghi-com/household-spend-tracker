"""Household colour schemes, and the rules that keep them usable.

Two households in one browser look identical, and that is how you post a
statement into the wrong one. A palette per household is a cheap, constant
signal of which ledger you are in -- it is on every screen, it needs no reading,
and you notice it changed before you notice anything else.

The usability rule is structural before it is numeric. **A household picks a
palette from this table and may override exactly one colour: the accent.**
Nothing can reach the text and background pairs, so no household can make its
own ledger unreadable -- that whole class of mistake is unavailable rather than
merely discouraged. The one free value is then checked against WCAG contrast
before it is stored, so the escape hatch cannot be used to reintroduce it.

Every palette in this table is checked by the same maths in the test suite, so a
shipped palette cannot fail a rule the app enforces on the user.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .errors import ValidationError

#: Text on its background, WCAG 2.1 AA for normal text. Everything in this app
#: is 13-15px, so the large-text allowance of 3.0 never applies.
TEXT_CONTRAST = 4.5
#: A colour that is a *shape* rather than a letter -- the accent behind a button,
#: a focus ring, the active chip. WCAG 2.1 AA for non-text contrast.
SHAPE_CONTRAST = 3.0


@dataclass(frozen=True, slots=True)
class Scheme:
    """One set of colours: what light mode or dark mode looks like."""

    ink: str
    muted: str
    paper: str
    surface: str
    surface_2: str
    line: str
    accent: str
    accent_ink: str
    danger: str
    warn: str
    positive: str

    def as_css(self) -> dict[str, str]:
        """The custom properties, named as the stylesheet names them."""
        return {
            "--ink": self.ink,
            "--muted": self.muted,
            "--paper": self.paper,
            "--surface": self.surface,
            "--surface-2": self.surface_2,
            "--line": self.line,
            "--accent": self.accent,
            "--accent-ink": self.accent_ink,
            "--danger": self.danger,
            "--warn": self.warn,
            "--positive": self.positive,
        }


@dataclass(frozen=True, slots=True)
class Palette:
    key: str
    label: str
    light: Scheme
    dark: Scheme


def _relative_luminance(hex_colour: str) -> float:
    """WCAG 2.1 relative luminance, from the sRGB definition."""
    value = hex_colour.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    channels = []
    for index in (0, 2, 4):
        raw = int(value[index : index + 2], 16) / 255
        channels.append(raw / 12.92 if raw <= 0.04045 else ((raw + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(one: str, two: str) -> float:
    """How far apart two colours are, 1.0 (identical) to 21.0 (black on white)."""
    first = _relative_luminance(one)
    second = _relative_luminance(two)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _valid_hex(value: str) -> str:
    text = value.strip()
    if not text.startswith("#"):
        text = f"#{text}"
    body = text[1:]
    if len(body) == 3:
        body = "".join(char * 2 for char in body)
    if len(body) != 6 or any(char not in "0123456789abcdefABCDEF" for char in body):
        raise ValidationError(f"{value!r} is not a colour like #15705c")
    return f"#{body.lower()}"


def ink_for(accent: str, scheme: Scheme) -> str:
    """What to write *on* the accent: whichever of the two reads better.

    Chosen rather than asked for. Somebody picking a colour is thinking about
    the colour, not about the label sitting on top of it, and getting this wrong
    is how a button ends up with a title nobody can read.
    """
    candidates = (scheme.paper, "#ffffff", scheme.ink)
    return max(candidates, key=lambda option: contrast_ratio(accent, option))


def _to_hsl(hex_colour: str) -> tuple[float, float, float]:
    value = _valid_hex(hex_colour)[1:]
    red, green, blue = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    high, low = max(red, green, blue), min(red, green, blue)
    lightness = (high + low) / 2
    if high == low:
        return 0.0, 0.0, lightness
    span = high - low
    saturation = span / (2 - high - low) if lightness > 0.5 else span / (high + low)
    if high == red:
        hue = ((green - blue) / span) % 6
    elif high == green:
        hue = (blue - red) / span + 2
    else:
        hue = (red - green) / span + 4
    return hue * 60, saturation, lightness


def _from_hsl(hue: float, saturation: float, lightness: float) -> str:
    chroma = (1 - abs(2 * lightness - 1)) * saturation
    second = chroma * (1 - abs(((hue / 60) % 2) - 1))
    base = lightness - chroma / 2
    sixth = int(hue // 60) % 6
    red, green, blue = [
        (chroma, second, 0.0), (second, chroma, 0.0), (0.0, chroma, second),
        (0.0, second, chroma), (second, 0.0, chroma), (chroma, 0.0, second),
    ][sixth]
    return "#" + "".join(f"{round((channel + base) * 255):02x}" for channel in (red, green, blue))


@dataclass(frozen=True, slots=True)
class AccentPlan:
    """What a chosen accent actually becomes, in each scheme."""

    chosen: str
    light: str
    light_ink: str
    dark: str
    dark_ink: str

    @property
    def adjusted(self) -> bool:
        """Did either scheme have to move away from what was picked?"""
        return self.light != self.chosen or self.dark != self.chosen


def _usable_in(hue: float, saturation: float, scheme: Scheme) -> str | None:
    """This hue at the weight the palette uses, adjusted until it passes.

    The target is the palette's *own* accent lightness for this scheme, not the
    one the household typed. A dark scheme wants a pale accent and a light
    scheme a deep one -- that is what every shipped palette does -- so aiming at
    the household's single figure lands one of the two schemes right on the
    contrast threshold, where it technically passes and looks muddy.

    Searched rather than nudged: stepping outward until something passes finds
    the first acceptable answer, which is not the same as the nearest one.
    """
    wanted = _to_hsl(scheme.accent)[2]
    best: tuple[float, str] | None = None
    for step in range(3, 98):
        candidate = _from_hsl(hue, saturation, step / 100)
        if contrast_ratio(candidate, ink_for(candidate, scheme)) < TEXT_CONTRAST:
            continue
        if contrast_ratio(candidate, scheme.surface) < SHAPE_CONTRAST:
            continue
        distance = abs(step / 100 - wanted)
        if best is None or distance < best[0]:
            best = (distance, candidate)
    return None if best is None else best[1]


def plan_accent(accent: str, palette: Palette) -> AccentPlan:
    """Work out what a chosen accent becomes, and refuse what cannot work.

    The household's colour is its **hue and saturation**. The brightness is
    the app's job,
    because one stored colour is shown on a white page and on a near-black one,
    and no single value does both -- `#15705c` is the shipped green and it
    vanishes into a dark background. So the hue and saturation are kept, and the
    hue is worn at the weight the palette itself uses in that scheme, adjusted
    until both contrast rules are satisfied.

    This is why an unusable theme cannot be made rather than merely being
    discouraged: there is no value a household can store that produces one. A
    hue with no workable lightness in some scheme is still refused, with the
    reason, rather than quietly turned into a colour nobody asked for.
    """
    tidy = _valid_hex(accent)
    hue, saturation, _ = _to_hsl(tidy)

    planned: dict[str, str] = {}
    for name, scheme in (("light", palette.light), ("dark", palette.dark)):
        found = _usable_in(hue, saturation, scheme)
        if found is None:
            raise ValidationError(
                f"no shade of {tidy} works in {name} mode -- it cannot be both readable "
                "under a label and visible against the page. Try a more saturated colour."
            )
        planned[name] = found

    return AccentPlan(
        chosen=tidy,
        light=planned["light"],
        light_ink=ink_for(planned["light"], palette.light),
        dark=planned["dark"],
        dark_ink=ink_for(planned["dark"], palette.dark),
    )


#: The palettes, in the order they are offered and auto-assigned.
#:
#: `moss` is first because it is what the app looked like before households had
#: colours, so an existing household keeps the face it had. Each one is a
#: different hue at a similar weight, so no household's ledger is harder to read
#: than another's -- the difference is meant to be recognisable, not decorative.
PALETTES: tuple[Palette, ...] = (
    Palette(
        key="moss",
        label="Moss",
        light=Scheme(
            ink="#16222a", muted="#566761", paper="#f4f7f4", surface="#ffffff",
            surface_2="#edf1ec", line="#d7e0d9", accent="#15705c", accent_ink="#ffffff",
            danger="#a3341f", warn="#7a4f0c", positive="#15705c",
        ),
        dark=Scheme(
            ink="#e6ede9", muted="#9daba5", paper="#0f1715", surface="#17201e",
            surface_2="#1d2724", line="#2a3733", accent="#5ac3a6", accent_ink="#0f1715",
            danger="#e08a72", warn="#dca85c", positive="#5ac3a6",
        ),
    ),
    Palette(
        key="slate",
        label="Slate",
        light=Scheme(
            ink="#1b2430", muted="#586576", paper="#f4f6f9", surface="#ffffff",
            surface_2="#eceff4", line="#d5dbe4", accent="#2f5d9e", accent_ink="#ffffff",
            danger="#a3341f", warn="#7a4f0c", positive="#1f6b52",
        ),
        dark=Scheme(
            ink="#e4eaf2", muted="#9aa6b6", paper="#101519", surface="#171d23",
            surface_2="#1d252c", line="#2a343d", accent="#7fb0f0", accent_ink="#101519",
            danger="#e08a72", warn="#dca85c", positive="#5fc09b",
        ),
    ),
    Palette(
        key="clay",
        label="Clay",
        light=Scheme(
            ink="#2b1f1a", muted="#6b5a50", paper="#faf5f1", surface="#ffffff",
            surface_2="#f2ebe5", line="#e0d5cb", accent="#9c4a24", accent_ink="#ffffff",
            danger="#a3341f", warn="#7a4f0c", positive="#1f6b52",
        ),
        dark=Scheme(
            ink="#f0e7e0", muted="#b3a196", paper="#171210", surface="#1f1815",
            surface_2="#271f1b", line="#362c26", accent="#e29065", accent_ink="#171210",
            danger="#e08a72", warn="#dca85c", positive="#5fc09b",
        ),
    ),
    Palette(
        key="indigo",
        label="Indigo",
        light=Scheme(
            ink="#1d1b33", muted="#5e5a7a", paper="#f6f5fb", surface="#ffffff",
            surface_2="#eeecf6", line="#dad7e8", accent="#54469b", accent_ink="#ffffff",
            danger="#a3341f", warn="#7a4f0c", positive="#1f6b52",
        ),
        dark=Scheme(
            ink="#e8e6f4", muted="#a29db8", paper="#121120", surface="#1a1828",
            surface_2="#221f33", line="#2f2c44", accent="#a99af0", accent_ink="#121120",
            danger="#e08a72", warn="#dca85c", positive="#5fc09b",
        ),
    ),
    Palette(
        key="plum",
        label="Plum",
        light=Scheme(
            ink="#2d1a26", muted="#6d5464", paper="#faf4f8", surface="#ffffff",
            surface_2="#f3e9ef", line="#e3d3dd", accent="#8d3466", accent_ink="#ffffff",
            danger="#a3341f", warn="#7a4f0c", positive="#1f6b52",
        ),
        dark=Scheme(
            ink="#f2e6ee", muted="#b59aab", paper="#181115", surface="#20181d", surface_2="#291f26",
            line="#382b33", accent="#dd8cbb", accent_ink="#181115",
            danger="#e08a72", warn="#dca85c", positive="#5fc09b",
        ),
    ),
    Palette(
        key="harbour",
        label="Harbour",
        light=Scheme(
            ink="#10262b", muted="#4f6a70", paper="#f1f7f8", surface="#ffffff",
            surface_2="#e7f0f2", line="#cfdfe2", accent="#146b7a", accent_ink="#ffffff",
            danger="#a3341f", warn="#7a4f0c", positive="#1f6b52",
        ),
        dark=Scheme(
            ink="#e0eef0", muted="#93a9ae", paper="#0d1618", surface="#141f21",
            surface_2="#1a2729", line="#26363a", accent="#62c0d2", accent_ink="#0d1618",
            danger="#e08a72", warn="#dca85c", positive="#5fc09b",
        ),
    ),
)

BY_KEY: dict[str, Palette] = {palette.key: palette for palette in PALETTES}
DEFAULT_KEY = PALETTES[0].key


def get(key: str | None) -> Palette:
    """The palette with this key, or the default. Never raises.

    A stored key that no longer exists -- a palette retired between releases --
    must not stop a household from opening. Losing your colour is a nuisance;
    losing your ledger is not.
    """
    return BY_KEY.get(key or "", PALETTES[0])


def next_palette(taken: list[str]) -> str:
    """A palette for a new household: the first nobody is using.

    Two households in one browser looking alike is the thing this feature
    exists to prevent, so a new one must not open wearing a colour already in
    use. Past the end of the table, pick the one used least.
    """
    for palette in PALETTES:
        if palette.key not in taken:
            return palette.key
    counts = {palette.key: taken.count(palette.key) for palette in PALETTES}
    return min(counts, key=lambda key: counts[key])


def resolve(theme: str | None, accent: str | None) -> tuple[Scheme, Scheme]:
    """The colours a household actually shows, light and dark.

    The palette, with the household's own accent worked in if it has one. This
    is the only place that combination is made, and the answer travels to the
    client as plain hex -- so the browser never re-implements the contrast
    rules, and there is no second copy of them to drift out of agreement with
    the one the tests check.
    """
    palette = get(theme)
    if not accent:
        return palette.light, palette.dark

    try:
        plan = plan_accent(accent, palette)
    except ValidationError:
        # Stored accents are checked on the way in. If one is somehow no longer
        # workable -- a palette retouched under it -- the household still opens,
        # wearing the palette's own colour. Never a blank page over a shade.
        return palette.light, palette.dark

    return (
        Scheme(**{**asdict(palette.light), "accent": plan.light, "accent_ink": plan.light_ink}),
        Scheme(**{**asdict(palette.dark), "accent": plan.dark, "accent_ink": plan.dark_ink}),
    )
