"""Generate the integration's brand images from the source artwork.

    python -m pip install Pillow
    python brands/make_brand_images.py

Writes six PNGs into custom_components/northumbrian_water/brand/.

Two sources, because the icon and the logo want different artwork:

* icon.*  <- nwl_logo_512x512.webp, the square "NW living water" lockup
* logo.*  <- nwl_desktop2x.webp, the landscape "NORTHUMBRIAN WATER living
             water" wordmark

Everything is built on the same idea: reduce the artwork to a *flat ink colour
plus a coverage mask*, then stamp the mask with whichever colour the theme
needs. That makes the dark variant an exact recolour rather than a filter, and
means resampling the mask can never introduce colour fringing. Only the way the
mask is obtained differs between the two sources.

Recovering the mask
-------------------
nwl_desktop2x.webp already has an alpha channel and its ink is pure white, so
the mask is just that alpha channel, used as-is. Nothing to reconstruct.

nwl_logo_512x512.webp has no alpha -- it is flat navy on a solid white box -- so
the mask has to be recovered by modelling each pixel as ink composited over
white at some coverage:

    p_c = a * INK_c + (1 - a) * 255

`a` is taken as the orthogonal projection of the pixel onto the white->INK line.
The obvious alternative -- solving exactly for `a` and a per-pixel colour -- is
underdetermined and, on this file, wrong: VP8's 4:2:0 chroma subsampling makes
the red channel fall faster than green and blue at edges, so an exact un-matte
recovers cyan (0, 240, 240) for partial-coverage pixels and bakes a cyan fringe
around every letter. Projecting onto the known line discards that artefact
instead of reproducing it. Measured against the source it costs a median error
of ~1/255 over the inked pixels, confined to the antialiased rim.

On shipping @2x
---------------
The @2x icons are not optional, despite nothing rendering large enough to need
them. Home Assistant resolves a missing brand image through the fallback chains
in homeassistant/components/brands/const.py, and the one for the dark hDPI icon
is:

    "dark_icon@2x.png": ["icon@2x.png", "icon.png"]

`dark_icon.png` is not in it. The integration detail page is the only caller
that asks for `icon@2x`, and in dark mode it asks for `dark_icon@2x.png` -- so
without the two @2x files that request falls all the way through to `icon.png`
and the page shows the navy mark on a dark background. The six files emitted
here cover every request the frontend can actually make. (`logo@2x` and
`dark_logo@2x` are never requested by any call site, so they are skipped.)
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "brands" / "source"
SRC_ICON = SOURCE / "nwl_logo_512x512.webp"
SRC_LOGO = SOURCE / "nwl_desktop2x.webp"
OUT = ROOT / "custom_components" / "northumbrian_water" / "brand"

INK = (0, 68, 150)  # #004496, the sole ink colour in the square lockup
WHITE = (255, 255, 255)
NOISE_FLOOR = 0.02  # coverage below this is VP8 ringing, not ink
ICON_PX = 256


def coverage_from_white_matte(img):
    """Ink coverage of art that has no alpha, as a float image in [0, 1]."""
    img = img.convert("RGB")
    w, h = img.size
    px = img.load()
    denom = sum((255 - c) ** 2 for c in INK)
    out = Image.new("F", (w, h))
    op = out.load()
    for y in range(h):
        for x in range(w):
            p = px[x, y]
            a = sum((255 - p[c]) * (255 - INK[c]) for c in range(3)) / denom
            op[x, y] = 0.0 if a < NOISE_FLOOR else min(a, 1.0)
    return out


def coverage_from_alpha(img):
    """Ink coverage of art that already carries an alpha channel."""
    return img.convert("RGBA").getchannel("A").convert("F").point(lambda v: v / 255)


def to_alpha(mask):
    """Float coverage -> 8-bit alpha, clamped (Lanczos can overshoot [0, 1])."""
    a8 = Image.new("L", mask.size)
    a8.putdata([max(0, min(255, int(v * 255 + 0.5))) for v in mask.getdata()])
    return a8


def trim(mask):
    """Drop the transparent border the sources pad the artwork with."""
    return mask.crop(to_alpha(mask).getbbox())


def emit(mask, colour, name):
    img = Image.new("RGBA", mask.size, colour + (0,))
    img.putalpha(to_alpha(mask))
    img.save(OUT / name, "PNG", optimize=True)
    print(f"  {name:<17} {img.size[0]}x{img.size[1]}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    # logo.*: the landscape wordmark, left at its native resolution. Its 78px
    # height is under the 128-256 the brands convention suggests, but the only
    # place HA draws a logo is the device page header at 30 CSS px, so it has
    # headroom on a 2x display and is only marginal on a 3x one.
    logo = trim(coverage_from_alpha(Image.open(SRC_LOGO)))
    emit(logo, INK, "logo.png")
    emit(logo, WHITE, "dark_logo.png")

    # icon.*: square, so pad the landscape lockup out before scaling down.
    mark = trim(coverage_from_white_matte(Image.open(SRC_ICON)))
    tw, th = mark.size
    side = max(tw, th)
    square = Image.new("F", (side, side), 0.0)
    square.paste(mark, ((side - tw) // 2, (side - th) // 2))

    # @2x is the padded square at its native size -- no resampling at all, so it
    # carries strictly more real detail than the 1x without inventing any. It is
    # not the conventional 512 because the source only holds 352px of mark.
    emit(square, INK, "icon@2x.png")
    emit(square, WHITE, "dark_icon@2x.png")

    icon = square.resize((ICON_PX, ICON_PX), Image.LANCZOS)
    emit(icon, INK, "icon.png")
    emit(icon, WHITE, "dark_icon.png")


if __name__ == "__main__":
    main()
