#!/usr/bin/env python3
"""
A backend/app/layout.py ujragenerasa egy koordinata-transzformacioval.

A layout.py gepi generalasu, ezert nem kezzel szerkesztjuk: betoltjuk a jelenlegi
mappinget, atvezetjuk rajta a transform() fuggvenyt, es ugyanabban a formatumban
irjuk vissza. Igy a kovetkezo valtoztatashoz is eleg csak a transform()-ot atirni.

    python tools/remap_layout.py            # kiirja, mi valtozna (dry run)
    python tools/remap_layout.py --write    # tenylegesen felulirja a layout.py-t

FIGYELEM: a script NEM idempotens -- a jelenlegi layout.py-ra alkalmazza a
transform()-ot. Az alabbi transzformacio mar le van futtatva, ujbol futtatva
megegyszer eltolna mindent. A kovetkezo valtoztatasnal ird at a transform()-ot
(es a mellette levo datumozott megjegyzest) az UJ valtoztatasra.
"""

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYOUT_PATH = os.path.join(REPO, "backend", "app", "layout.py")

PANEL_WIDTH = 48
PANEL_HEIGHT = 96

# -------------------------------------------------------------------
# A TRANSZFORMACIO
# -------------------------------------------------------------------
# 2026-10-06:
#   1. Minden pixel eggyel lejjebb -> a felso res 3 helyett 4 sor.
#   2. A jobb fel (x >= 25) vegig eggyel balra. A bal fel helyben marad, igy a
#      liftakna rese a teljes magassagban 1 oszlop szeles (csak a 23-as), a
#      47-es oszlop pedig mindenhol felszabadul.

RIGHT_HALF_FROM_X = 25  # ettol az (eredeti) oszloptol szamit jobb felnek


def transform(x, y):
    """Egy eredeti fizikai koordinatat kepez az uj fizikai koordinatara."""
    ny = y + 1
    nx = x - 1 if x >= RIGHT_HALF_FROM_X else x
    return nx, ny


# -------------------------------------------------------------------

HEADER = """# Auto-generated Sch\u00f6nherz Matrix Layout Mappings
# 0-indexed coordinates
# Width = {w} (X: 0..{wm})
# Height = {h} (Y: 0..{hm})

# Mapping from 32x26 animation coordinates: (anim_x, anim_y) -> (physical_x, physical_y)
ANIMATABLE_MAPPING = [
"""


def load_layout():
    sys.path.insert(0, os.path.join(REPO, "backend"))
    from app.layout import ANIMATABLE_MAPPING, STATIC_PIXELS
    return list(ANIMATABLE_MAPPING), list(STATIC_PIXELS)


def render_layout(anim, static):
    out = [HEADER.format(w=PANEL_WIDTH, wm=PANEL_WIDTH - 1,
                         h=PANEL_HEIGHT, hm=PANEL_HEIGHT - 1)]
    for (ax, ay), (px, py) in anim:
        out.append("    ((%d, %d), (%d, %d)),\n" % (ax, ay, px, py))
    out.append("]\n\n")
    out.append("# List of physical (x, y) coordinates for static 'x' pixels\n")
    out.append("STATIC_PIXELS = [\n")
    for px, py in static:
        out.append("    (%d, %d),\n" % (px, py))
    out.append("]\n")
    return "".join(out)


def check(label, anim, static):
    """Alapveto epsegvizsgalat: nincs atfedes, minden a panelen belul van."""
    problems = []
    anim_phys = [p for _, p in anim]
    all_phys = anim_phys + list(static)
    for x, y in all_phys:
        if not (0 <= x < PANEL_WIDTH and 0 <= y < PANEL_HEIGHT):
            problems.append("panelen kivul: (%d, %d)" % (x, y))
    if len(set(all_phys)) != len(all_phys):
        problems.append("atfedo fizikai pixelek (%d db ismetlodes)"
                        % (len(all_phys) - len(set(all_phys))))
    if len(set(a for a, _ in anim)) != len(anim):
        problems.append("ismetlodo animacio-koordinata")
    print("%s: %d anim + %d statikus = %d pixel" % (label, len(anim), len(static), len(all_phys)))
    axs = sorted(set(x for x, _ in all_phys))
    ays = sorted(set(y for _, y in all_phys))
    print("  oszlopok (%d): %s" % (len(axs), axs))
    print("  sorok (%d): %s" % (len(ays), ays))
    print("  szabad oszlopok: %s" % [x for x in range(PANEL_WIDTH) if x not in set(axs)])
    for p in problems:
        print("  [HIBA] %s" % p)
    return not problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="a layout.py tenyleges felulirasa")
    args = ap.parse_args()

    anim, static = load_layout()
    print("=== ELOTTE ===")
    check("jelenlegi", anim, static)

    new_anim = [(a, transform(*p)) for a, p in anim]
    new_static = [transform(*p) for p in static]

    print("\n=== UTANA ===")
    if not check("uj", new_anim, new_static):
        print("\nA transzformacio hibas allapotot adna, nem irok semmit.")
        return 1

    text = render_layout(new_anim, new_static)
    if args.write:
        with open(LAYOUT_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("\nlayout.py felulirva (%d sor)." % text.count("\n"))
    else:
        print("\n(dry run \u2014 a --write kapcsoloval irja felul a layout.py-t)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
