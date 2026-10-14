"""Rasterise the tab mark, because not every browser takes an SVG favicon.

`client/public/favicon.svg` is the drawing. Safari will not use it, and a PWA
manifest needs raster sizes whatever the browser -- so this script redraws the
*same geometry* with Pillow and writes the files the `<link>` tags and
`manifest.json` point at.

Redrawn rather than converted on purpose: rasterising the SVG would mean a
dependency (cairosvg, or a system rsvg) that nothing else here needs, on a
drawing that is four shapes. The cost is that the two have to be kept in step
by hand, which is why the geometry below is written in the SVG's own 32-unit
coordinates and scaled once -- a change to the SVG is a change to the same
numbers here.

Run it when the mark changes; the output is committed, so a build never needs
Pillow:

    ./.venv/bin/python -m scripts.make_icons
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

#: Where the files land. `client/public` is copied to the root of the build, so
#: `/favicon.svg` and friends are served by the SPA fallback in `main.py`.
PUBLIC = Path(__file__).resolve().parent.parent / "client" / "public"

#: The default palette's accent, and the ink on the slip. Both from
#: `client/src/styles.css` -- a favicon cannot read a household's own colour.
ACCENT = (0x15, 0x70, 0x5C, 0xFF)
PAPER = (0xFF, 0xFF, 0xFF, 0xFF)

#: The SVG's viewBox. Every number below is in these units.
UNITS = 32
RADIUS = 7
#: The slip: left, top, right, and the foot the notches are cut from.
SLIP = (9, 7, 23, 25)
#: Five notch points across the foot, alternating up and down by this much.
NOTCH = 2
#: x, y, width, height for each rule on the slip.
RULES = ((12, 11, 8, 2), (12, 15, 5, 2))

#: What gets written. 32 is the tab, 180 is Apple's home-screen size, 192 and
#: 512 are what a web manifest is expected to carry.
SIZES = {"favicon-32.png": 32, "apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}

#: The ICO carries the small sizes only. It exists for browsers that ask for
#: `/favicon.ico` without being told to, which is all of them.
ICO_SIZES = (16, 32, 48)


def draw(px: int) -> Image.Image:
    """The mark at `px` square, drawn at 4x and reduced so the curves are clean."""
    scale = 4
    size = px * scale
    unit = size / UNITS
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)

    def u(value: float) -> float:
        return value * unit

    pen.rounded_rectangle((0, 0, size - 1, size - 1), radius=u(RADIUS), fill=ACCENT)

    left, top, right, foot = SLIP
    # The foot is the zigzag: six points across, every other one lifted, which
    # is the `l` run in the SVG path written out.
    step = (right - left) / 6
    zigzag = [(u(right - step * n), u(foot - (NOTCH if n % 2 else 0))) for n in range(7)]
    pen.polygon([(u(left), u(top)), (u(right), u(top)), *zigzag], fill=PAPER)

    for x, y, width, height in RULES:
        pen.rounded_rectangle(
            (u(x), u(y), u(x + width), u(y + height)), radius=u(height / 2), fill=ACCENT
        )

    return image.resize((px, px), Image.LANCZOS)


#: The desktop app's icons (#61), which electron-builder reads from here.
DESKTOP = Path(__file__).resolve().parent.parent / "desktop" / "build-resources"

#: The tiles an MSIX package names in its manifest, by electron-builder's
#: file names, at their 100% sizes. Windows scales the rest from these.
APPX = {
    "StoreLogo.png": (50, 50),
    "Square44x44Logo.png": (44, 44),
    "Square150x150Logo.png": (150, 150),
    "Wide310x150Logo.png": (310, 150),
}


def placed(width: int, height: int, mark: int) -> Image.Image:
    """The mark at `mark` px, centred on a transparent `width` x `height`."""
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(draw(mark), ((width - mark) // 2, (height - mark) // 2))
    return canvas


def desktop() -> None:
    """The Mac icon, the Windows icon and the MSIX tiles."""
    (DESKTOP / "appx").mkdir(parents=True, exist_ok=True)
    # Apple's grid leaves a margin round the shape: 824 of 1024.
    mac = placed(1024, 1024, 824)
    mac.save(DESKTOP / "icon.png")
    mac.save(DESKTOP / "icon.icns")
    draw(256).save(DESKTOP / "icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    for name, (width, height) in APPX.items():
        placed(width, height, min(width, height)).save(DESKTOP / "appx" / name)
    print(f"wrote the desktop icons into {DESKTOP}")


def main() -> None:
    desktop()
    PUBLIC.mkdir(parents=True, exist_ok=True)
    for name, px in SIZES.items():
        draw(px).save(PUBLIC / name)
        print(f"wrote {name} ({px}px)")
    # One file holding all three, so a browser picks the size it wants.
    draw(max(ICO_SIZES)).save(PUBLIC / "favicon.ico", sizes=[(n, n) for n in ICO_SIZES])
    print(f"wrote favicon.ico ({', '.join(str(n) for n in ICO_SIZES)})")


if __name__ == "__main__":
    main()
