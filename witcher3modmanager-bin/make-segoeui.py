#!/usr/bin/env python3
"""Builds a Segoe UI stand-in from Liberation Sans.

Script Merger asks GDI+ for "Segoe UI" and frees the list font when the
family it gets back has a different name, so a substituted font crashes it.
"""

import sys

from fontTools.ttLib import TTFont

SRC = "/usr/share/fonts/liberation/LiberationSans-{}.ttf"
STYLES = {
    "Regular": ("segoeui.ttf", "Regular"),
    "Bold": ("segoeuib.ttf", "Bold"),
    "Italic": ("segoeuii.ttf", "Italic"),
    "BoldItalic": ("segoeuiz.ttf", "Bold Italic"),
}

out_dir = sys.argv[1] if len(sys.argv) > 1 else "."

for style, (filename, subfamily) in STYLES.items():
    font = TTFont(SRC.format(style))
    names = font["name"]
    full_name = "Segoe UI" if subfamily == "Regular" else f"Segoe UI {subfamily}"
    names.removeNames(nameID=16)
    names.removeNames(nameID=17)
    for name_id, value in (
        (1, "Segoe UI"),
        (2, subfamily),
        (3, f"SegoeUI-Wine-{style}"),
        (4, full_name),
        (6, f"SegoeUI-{style}"),
    ):
        names.setName(value, name_id, 3, 1, 0x409)
        names.setName(value, name_id, 1, 0, 0)
    font.save(f"{out_dir}/{filename}")
