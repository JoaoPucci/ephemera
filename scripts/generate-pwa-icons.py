#!/usr/bin/env python3
"""Rasterize the SVG icon masters into the PNGs the PWA manifest references.

Run this whenever an SVG master under app/static/icons/ changes (glyph
redraw, palette tweak, cap-height adjustment). The PNGs it produces are
checked in alongside the masters so deploys don't need a build step.

Inputs (in app/static/icons/):
  icon-master-any-light.svg       full-bleed disc, dark bg, light glyph
  icon-master-any-dark.svg        full-bleed disc, light bg, dark glyph
  icon-master-maskable-light.svg  full-bleed rect, dark bg, light glyph
  icon-master-maskable-dark.svg   full-bleed rect, light bg, dark glyph

Outputs (in app/static/icons/):
  icon-{purpose}-{theme}-{192,512}.png  manifest icon entries
  apple-touch-icon.png                  iOS Add-to-Home-Screen (180px,
                                        always the any-light variant per
                                        designer brief: dark-bg/light-
                                        glyph stands out on busy
                                        wallpapers; iOS doesn't honour
                                        prefers-color-scheme on touch
                                        icons).

Backend: librsvg (via PyGObject) + cairo. Both ship with the system
packages already installed in the dev environment (gir1.2-rsvg-2.0,
python3-gi, python3-cairo). No extra pip dependency.
"""

import sys
from pathlib import Path

import cairo
import gi

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg  # noqa: E402

ICONS_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"

MASTERS = [
    "icon-master-any-light.svg",
    "icon-master-any-dark.svg",
    "icon-master-maskable-light.svg",
    "icon-master-maskable-dark.svg",
]
MANIFEST_SIZES = (192, 512)
APPLE_TOUCH_SIZE = 180
APPLE_TOUCH_SOURCE = "icon-master-any-light.svg"


def rasterize(svg_path: Path, png_path: Path, size: int) -> None:
    handle = Rsvg.Handle.new_from_file(str(svg_path))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    viewport = Rsvg.Rectangle()
    viewport.x = 0
    viewport.y = 0
    viewport.width = size
    viewport.height = size
    ok = handle.render_document(ctx, viewport)
    if not ok:
        raise RuntimeError(f"librsvg refused to render {svg_path}")
    surface.write_to_png(str(png_path))


def main() -> int:
    if not ICONS_DIR.is_dir():
        print(f"icons dir missing: {ICONS_DIR}", file=sys.stderr)
        return 1

    produced: list[Path] = []

    for master in MASTERS:
        svg = ICONS_DIR / master
        if not svg.is_file():
            print(f"master missing: {svg}", file=sys.stderr)
            return 1
        # icon-master-{purpose}-{theme}.svg -> icon-{purpose}-{theme}-{size}.png
        stem = svg.stem.removeprefix("icon-master-")
        for size in MANIFEST_SIZES:
            out = ICONS_DIR / f"icon-{stem}-{size}.png"
            rasterize(svg, out, size)
            produced.append(out)

    apple_src = ICONS_DIR / APPLE_TOUCH_SOURCE
    apple_out = ICONS_DIR / "apple-touch-icon.png"
    rasterize(apple_src, apple_out, APPLE_TOUCH_SIZE)
    produced.append(apple_out)

    for p in produced:
        print(f"wrote {p.relative_to(ICONS_DIR.parent.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
