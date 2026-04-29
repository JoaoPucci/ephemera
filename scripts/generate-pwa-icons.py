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
  apple-touch-icon.png                  iOS Add-to-Home-Screen, prod
                                        posture: 180px any-light master
                                        (dark-bg/light-glyph -- stands
                                        out on busy wallpapers).
  apple-touch-icon-light.png            iOS Add-to-Home-Screen, non-prod
                                        posture: 180px any-DARK master
                                        (light-bg/dark-glyph -- visually
                                        the inverse of the prod tile so
                                        a dev / staging install on the
                                        same phone is at-a-glance
                                        distinguishable). Wired in by
                                        EPHEMERA_DEPLOYMENT_LABEL via
                                        template_context (see
                                        app/i18n.py).

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
# Two apple-touch-icons: prod ships the visually-dark master (any-light
# in our naming -- "light" describes the OS theme it serves, not the
# tile's appearance), non-prod ships the visually-light inverse so the
# two installs are at-a-glance different on the home screen.
APPLE_TOUCH_OUTPUTS = {
    "apple-touch-icon.png": "icon-master-any-light.svg",
    "apple-touch-icon-light.png": "icon-master-any-dark.svg",
}


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

    for out_name, src_name in APPLE_TOUCH_OUTPUTS.items():
        src = ICONS_DIR / src_name
        out = ICONS_DIR / out_name
        rasterize(src, out, APPLE_TOUCH_SIZE)
        produced.append(out)

    for p in produced:
        print(f"wrote {p.relative_to(ICONS_DIR.parent.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
