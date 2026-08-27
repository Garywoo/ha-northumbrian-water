# Brand artwork

`source/` holds the raw artwork. `make_brand_images.py` turns it into the files
Home Assistant actually serves, in `custom_components/northumbrian_water/brand/`.

## How Home Assistant finds them

Since **HA 2026.3** a custom integration can ship its own brand images in a
`brand/` folder next to `manifest.json`:

```
custom_components/northumbrian_water/
├── __init__.py
├── manifest.json
└── brand/
    ├── icon.png
    ├── dark_icon.png
    ├── logo.png
    └── dark_logo.png
```

Home Assistant serves them from `/api/brands/integration/northumbrian_water/…`
and they take priority over `brands.home-assistant.io`. Nothing is declared in
`manifest.json`. This is core HA behaviour — not a HACS feature — and works the
same for an integration copied into `config/` by hand.

Recognised filenames (any subset, all optional):

| Light | Dark |
| --- | --- |
| `icon.png` | `dark_icon.png` |
| `icon@2x.png` | `dark_icon@2x.png` |
| `logo.png` | `dark_logo.png` |
| `logo@2x.png` | `dark_logo@2x.png` |

If only `icon.png` is supplied, HA uses it everywhere.

Sizes follow the [brands repo](https://github.com/home-assistant/brands)
convention: `icon.png` square at 256x256, `@2x` at 512x512, `logo.png`
landscape with the shortest side 128-256. PNG, transparent, trimmed of dead
space. Nothing validates this locally, so the numbers are guidance, not a gate.

This replaces the old route of opening a PR against `home-assistant/brands`
under `custom_integrations/`, which is no longer accepted for custom
integrations.

## What ships

Two sources, because the square icon and the landscape logo want different
artwork.

| File | Size | Ink | From |
| --- | --- | --- | --- |
| `icon.png` | 256x256 | `#004496` | `nwl_logo_512x512.webp` |
| `dark_icon.png` | 256x256 | white | `nwl_logo_512x512.webp` |
| `icon@2x.png` | 352x352 | `#004496` | `nwl_logo_512x512.webp` |
| `dark_icon@2x.png` | 352x352 | white | `nwl_logo_512x512.webp` |
| `logo.png` | 320x78 | `#004496` | `nwl_desktop2x.webp` |
| `dark_logo.png` | 320x78 | white | `nwl_desktop2x.webp` |

Everything reduces the artwork to a *flat ink colour plus a coverage mask*, then
stamps the mask with whichever colour the theme needs. That makes the dark
variant an exact recolour rather than a filter, and means resampling the mask
can never introduce colour fringing. Only how the mask is obtained differs.

**`nwl_desktop2x.webp`** — the "NORTHUMBRIAN WATER living water" wordmark — is
already RGBA with pure white ink, so the mask is simply its alpha channel, used
as-is. Nothing to reconstruct.

**`nwl_logo_512x512.webp`** — the square "NW living water" lockup — is lossy VP8
with **no alpha**: flat navy on a solid white box. The white has to be keyed out
first. `make_brand_images.py` recovers the mask by projecting each pixel onto
the white→ink line; the docstring explains why, rather than solving for an exact
un-matte (short version: 4:2:0 chroma subsampling makes an exact un-matte
produce cyan edge fringing).

### Sizes

`logo.*` is the wordmark trimmed to 320x78 and left at native resolution. Its
78px height is under the 128–256 the brands convention suggests for a logo, but
the only place HA draws one is the device page header at `height: 30px` — 60
device px on a 2x display, so there is headroom there and it is only marginal on
a 3x one. Upscaling would invent detail rather than add it.

`icon.*` pads the square lockup (trimmed to 352x236) out to 352x352 and scales
down to 256, so it carries vertical dead space — unavoidable for a landscape
lockup in a square frame. It stays legible down to about 32px; below ~40px the
"living water" script reads as texture rather than letters. If the small sizes
matter more than completeness, cropping `icon.*` to just the **NW** monogram
would hit harder — the generator would need a hand-picked crop box for that.

### Why `@2x` is not optional

Nothing renders large enough to need an hDPI icon — the biggest the icon is ever
drawn is the integration detail page header at 80 CSS px, 160 device px on a 2x
display, which the 256px `icon.png` already covers. The `@2x` files exist for a
different reason: **fallback chains**.

HA resolves a missing brand image through `IMAGE_FALLBACKS` in
`homeassistant/components/brands/const.py`. The chain for the dark hDPI icon is:

```python
"dark_icon@2x.png": ["icon@2x.png", "icon.png"]
```

`dark_icon.png` is *not* in it. The integration detail page is the only caller
that asks for `icon@2x`, and in dark mode it asks for `dark_icon@2x.png` — so
with only the 1x pair shipped, that request falls all the way through to
`icon.png` and the page shows the navy mark on a dark background. Shipping the
`@2x` pair is what fixes it.

They are 352x352, not the conventional 512x512, because that is the padded
square at its native size: strictly more real detail than the 1x, with no
resampling and nothing invented. Nothing validates the size locally, and 352
comfortably exceeds the ~160 device px actually drawn.

`logo@2x` and `dark_logo@2x` are skipped — no call site requests them.

## Where each file is used

`icon.*` does nearly all the work: 43 of the 45 brand-image call sites in the
frontend ask for it — the integrations dashboard, the add-integration list, the
device list and device page, config flow, repairs, logbook, related items,
target and device pickers, backup and energy screens.

`logo.*` is used in exactly two places, only one of which applies here: the
device page header, drawn 30px tall. (The other is a Matter commissioning
dialog.) Shipping it is close to cosmetic, but it is one line of the generator
and it renders the lockup at its natural aspect rather than boxed into a
square.

The six files cover every request the frontend can actually issue — the four
`brandsUrl` types (`icon`, `icon@2x`, `logo`, `logo@2x`) crossed with
`darkOptimized`, minus the `logo@2x` pair that has no call site. Worth
re-checking against `IMAGE_FALLBACKS` if a file is ever dropped: the chains do
not always degrade to the same theme.

## Regenerating

```
python -m pip install Pillow
python brands/make_brand_images.py
```

Deterministic — re-running reproduces the committed PNGs byte for byte.

## Superseded source

`nwl_square_192x192.png` is the blue-green droplet, the best square artwork on
nwl.co.uk before the 512px lockup turned up. Unused.

`nwl_landscape_321x79.png` is redundant: its alpha channel is bit-identical to
`nwl_desktop2x.webp`, so it is the same asset in a different container. Safe to
delete.

## Trademark

The logo is Northumbrian Water's. This integration is unofficial and
unaffiliated, as the main README says. The icon identifies which service the
integration talks to; it does not suggest NWL endorses it.
