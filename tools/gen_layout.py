#!/usr/bin/env python3
"""
A backend/app/layout.py generalasa a tervezoi Excel tablabol.

Az Excel a homlokzat igazsaga: egy cella = egy fizikai LED.
  - oszlopbetu -> fizikai x   (A=0, B=1, C=2, ...)
  - sorszam    -> fizikai y   (az Excel 1-indexelt, ezert y = sor - 1)
  - "p" cella  -> animalhato pixel (az animacio 32x26-os kepebol kap szint)
  - "x" cella  -> statikus pixel (animacio alatt sotet, idle-ben vilagithat)
  - minden mas cella (jegyzet, nyil, felirat) figyelmen kivul marad

Az animacio-koordinatakat a script szamolja: a "p" cellak balrol jobbra,
fentrol lefele sorrendben kapjak a (anim_x, anim_y) parokat, amibol pontosan
32 x 26 racsnak kell kijonnie.

    python tools/gen_layout.py                          # dry run
    python tools/gen_layout.py --write                  # layout.py kiirasa
    python tools/gen_layout.py --xlsx "masik.xlsx"      # mas forrasfajl

openpyxl nem kell hozza, a .xlsx egy zip + XML.
"""

import argparse
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_XLSX = os.path.join(REPO, "schonherz matrix terv.xlsx")
LAYOUT_PATH = os.path.join(REPO, "backend", "app", "layout.py")

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

PANEL_WIDTH = 48
PANEL_HEIGHT = 96
ANIM_WIDTH = 32
ANIM_HEIGHT = 26

HEADER = """# Auto-generated Schönherz Matrix Layout Mappings
# Forras: {src}  (tools/gen_layout.py)
# 0-indexed coordinates
# Width = {w} (X: 0..{wm})
# Height = {h} (Y: 0..{hm})

# Mapping from {aw}x{ah} animation coordinates: (anim_x, anim_y) -> (physical_x, physical_y)
ANIMATABLE_MAPPING = [
"""


def col_to_idx(ref):
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_cells(path):
    """{(x, y): szoveg} a munkalap nem ures cellaibol."""
    z = zipfile.ZipFile(path)

    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))

    cells = {}
    root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    for c in root.iter(NS + "c"):
        ref, t = c.get("r"), c.get("t")
        v = c.find(NS + "v")
        if t == "inlineStr":
            node = c.find(NS + "is")
            text = "".join(x.text or "" for x in node.iter(NS + "t")) if node is not None else ""
        elif v is None:
            continue
        elif t == "s":
            text = shared[int(v.text)]
        else:
            text = v.text
        text = (text or "").strip()
        if text:
            cells[(col_to_idx(ref), int(re.search(r"(\d+)$", ref).group(1)) - 1)] = text
    return cells


def build(cells):
    """A cellakbol ANIMATABLE_MAPPING + STATIC_PIXELS."""
    anim_px = sorted((y, x) for (x, y), v in cells.items() if v.lower() == "p")
    # sorfolytonos rendezes (fentrol le, balrol jobbra), ahogy az eredeti layout.py-ban
    static = sorted(((x, y) for (x, y), v in cells.items() if v.lower() == "x"),
                    key=lambda p: (p[1], p[0]))

    # az animalhato pixelek racsa: egyedi oszlopok es sorok sorrendben
    cols = sorted(set(x for _, x in anim_px))
    rows = sorted(set(y for y, _ in anim_px))
    col_idx = {c: i for i, c in enumerate(cols)}
    row_idx = {r: i for i, r in enumerate(rows)}

    mapping = []
    for y, x in anim_px:
        mapping.append(((col_idx[x], row_idx[y]), (x, y)))
    mapping.sort(key=lambda e: (e[0][1], e[0][0]))   # anim_y, majd anim_x szerint
    return mapping, static, cols, rows


def check(mapping, static, cols, rows, cells):
    problems = []
    if len(cols) != ANIM_WIDTH:
        problems.append("az animalhato racs %d oszlop szeles, nem %d" % (len(cols), ANIM_WIDTH))
    if len(rows) != ANIM_HEIGHT:
        problems.append("az animalhato racs %d sor magas, nem %d" % (len(rows), ANIM_HEIGHT))
    if len(mapping) != ANIM_WIDTH * ANIM_HEIGHT:
        problems.append("%d animalhato pixel, nem %d (lyukas a racs?)"
                        % (len(mapping), ANIM_WIDTH * ANIM_HEIGHT))
    if len(set(a for a, _ in mapping)) != len(mapping):
        problems.append("ismetlodo animacio-koordinata")

    all_phys = [p for _, p in mapping] + list(static)
    if len(set(all_phys)) != len(all_phys):
        problems.append("ugyanaz a fizikai pixel egyszerre p es x")
    for x, y in all_phys:
        if not (0 <= x < PANEL_WIDTH and 0 <= y < PANEL_HEIGHT):
            problems.append("panelen kivuli pixel: (%d, %d)" % (x, y))

    print("animalhato : %d pixel, %d oszlop x %d sor" % (len(mapping), len(cols), len(rows)))
    print("statikus   : %d pixel" % len(static))
    print("osszesen   : %d pixel" % len(all_phys))

    bycol = {}
    for x, y in static:
        bycol.setdefault(x, []).append(y)
    groups = {}
    for x in sorted(bycol):
        groups.setdefault(tuple(sorted(bycol[x])), []).append(x)
    print("statikus sorok oszloponkent (%d fele):" % len(groups))
    for rws, cls in sorted(groups.items(), key=lambda kv: kv[1][0]):
        print("   oszlop %-44s -> %s" % (str(cls)[:44], list(rws)))

    ignored = Counter(v for v in cells.values() if v.lower() not in ("p", "x"))
    if ignored:
        print("figyelmen kivul hagyott cellak: %d db, %d fele ertek"
              % (sum(ignored.values()), len(ignored)))

    for p in problems:
        print("  [HIBA] %s" % p)
    return not problems


def render(mapping, static, src_name):
    out = [HEADER.format(src=src_name, w=PANEL_WIDTH, wm=PANEL_WIDTH - 1,
                         h=PANEL_HEIGHT, hm=PANEL_HEIGHT - 1,
                         aw=ANIM_WIDTH, ah=ANIM_HEIGHT)]
    for (ax, ay), (px, py) in mapping:
        out.append("    ((%d, %d), (%d, %d)),\n" % (ax, ay, px, py))
    out.append("]\n\n")
    out.append("# List of physical (x, y) coordinates for static 'x' pixels\n")
    out.append("STATIC_PIXELS = [\n")
    for px, py in static:
        out.append("    (%d, %d),\n" % (px, py))
    out.append("]\n")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="layout.py generalasa a tervezoi Excelbol.")
    ap.add_argument("--xlsx", default=DEFAULT_XLSX, help="forras .xlsx")
    ap.add_argument("--write", action="store_true", help="a layout.py tenyleges kiirasa")
    args = ap.parse_args()

    if not os.path.exists(args.xlsx):
        print("Nincs meg a forrasfajl: %s" % args.xlsx)
        return 1

    print("Forras: %s" % args.xlsx)
    cells = read_cells(args.xlsx)
    mapping, static, cols, rows = build(cells)
    if not check(mapping, static, cols, rows, cells):
        print("\nA tabla hibas, nem irok semmit.")
        return 1

    text = render(mapping, static, os.path.basename(args.xlsx))
    if args.write:
        with open(LAYOUT_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("\nlayout.py kiirva (%d sor)." % text.count("\n"))
    else:
        print("\n(dry run — a --write kapcsoloval irja ki a layout.py-t)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
