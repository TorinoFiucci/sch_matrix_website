#!/usr/bin/env python3
"""
Teszt .q4x animacio generator a Schonherz Matrixhoz.

A formatumot a q4x_info.txt / backend/app/parser.py szerint irja ki:

    b"Q4X1" | uint16 width | uint16 height | uint32 qp4_size | qp4
            | uint32 qpr_zlib_size | qpr_zlib | uint32 audio_size (0)

Minden egesz big-endian. A QPR stream 4 fejlecsora utan kepkockankent
2496 bajt nyers RGB (32*26*3, sorfolytonos) + egy uint32 ms idotartam.

Hasznalat:
    python tools/gen_q4x.py --fps 25 --duration 30 --out test_25fps_30s.q4x
"""

import argparse
import math
import os
import struct
import sys
import zlib

WIDTH = 32
HEIGHT = 26
FRAME_PIXEL_SIZE = WIDTH * HEIGHT * 3

# -------------------------------------------------------------------
# Rajz-segedletek
# -------------------------------------------------------------------

def new_frame():
    return bytearray(FRAME_PIXEL_SIZE)


def put(buf, x, y, r, g, b):
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        o = (y * WIDTH + x) * 3
        buf[o] = r & 0xFF
        buf[o + 1] = g & 0xFF
        buf[o + 2] = b & 0xFF


def fill_rect(buf, x0, y0, w, h, color):
    r, g, b = color
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            put(buf, x, y, r, g, b)


def hsv(h, s, v):
    """h: 0..1 korbeforgo, s/v: 0..1 -> (r, g, b) 0..255"""
    h = h % 1.0
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i %= 6
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return int(r * 255), int(g * 255), int(b * 255)


# 3x5-os szamjegyek a visszaszamlalashoz (csak 1, 2, 3 kell)
DIGITS = {
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
}


def draw_digit(buf, ch, cx, cy, scale, color):
    """A ch szamjegyet a (cx, cy) kozeppont kore rajzolja, scale-szeresen."""
    glyph = DIGITS.get(ch)
    if not glyph:
        return
    gw, gh = 3 * scale, 5 * scale
    x0 = cx - gw // 2
    y0 = cy - gh // 2
    for gy, row in enumerate(glyph):
        for gx, cell in enumerate(row):
            if cell == "1":
                fill_rect(buf, x0 + gx * scale, y0 + gy * scale, scale, scale, color)


# -------------------------------------------------------------------
# A teszt-animacio szakaszai
# -------------------------------------------------------------------

def sect_color_sweep(buf, u, t, idx):
    """0-16%: R -> G -> B -> feher, felulrol lefele betoltve.
    Csatornasorrend (RGB vs BGR) es fenyero ellenorzese."""
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
    n = len(colors)
    step = min(int(u * n), n - 1)
    local = u * n - step
    color = colors[step]
    prev = colors[step - 1] if step > 0 else (0, 0, 0)
    split = int(local * HEIGHT)
    fill_rect(buf, 0, 0, WIDTH, split, color)
    fill_rect(buf, 0, split, WIDTH, HEIGHT - split, prev)


def sect_line_scan(buf, u, t, idx):
    """16-33%: 1 px fuggoleges vonal balrol jobbra, majd vizszintes fentrol le.
    Az ANIMATABLE_MAPPING sor/oszlop helyesseget mutatja."""
    if u < 0.5:
        x = int((u / 0.5) * WIDTH) % WIDTH
        for y in range(HEIGHT):
            put(buf, x, y, 255, 255, 255)
        # halvany szel-jelolok, hogy a levagott szelek is lathatoak legyenek
        for y in range(HEIGHT):
            put(buf, 0, y, 40, 0, 0)
            put(buf, WIDTH - 1, y, 0, 0, 40)
    else:
        y = int(((u - 0.5) / 0.5) * HEIGHT) % HEIGHT
        for x in range(WIDTH):
            put(buf, x, y, 255, 255, 255)
        for x in range(WIDTH):
            put(buf, x, 0, 40, 0, 0)
            put(buf, x, HEIGHT - 1, 0, 0, 40)


def sect_checker(buf, u, t, idx):
    """33-53%: mozgo sakktabla. Sorkimaradas es tearing lathato tole."""
    size = 2
    off = int(t * 6)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if (((x + off) // size) + ((y + off) // size)) % 2 == 0:
                put(buf, x, y, 0, 200, 255)
            else:
                put(buf, x, y, 20, 20, 20)


def sect_plasma(buf, u, t, idx):
    """53-73%: mozgo szivarvany-plazma. Szinatmenetek, banding."""
    for y in range(HEIGHT):
        for x in range(WIDTH):
            v = (
                math.sin(x * 0.30 + t * 2.0)
                + math.sin(y * 0.22 - t * 1.4)
                + math.sin((x + y) * 0.18 + t * 1.1)
            )
            r, g, b = hsv(v / 6.0 + t * 0.10, 1.0, 1.0)
            put(buf, x, y, r, g, b)


def sect_markers(buf, u, t, idx):
    """73-90%: keret + sarokjelolok + villogo kozepkereszt.
    Levagott szelek, eltolodott vagy tukrozott mapping."""
    on = (idx // 6) % 2 == 0
    for x in range(WIDTH):
        put(buf, x, 0, 255, 255, 0)
        put(buf, x, HEIGHT - 1, 255, 255, 0)
    for y in range(HEIGHT):
        put(buf, 0, y, 255, 255, 0)
        put(buf, WIDTH - 1, y, 255, 255, 0)
    # minden sarok mas szinu, igy a tukrozes/forgatas is kiderul
    fill_rect(buf, 0, 0, 3, 3, (255, 0, 0))
    fill_rect(buf, WIDTH - 3, 0, 3, 3, (0, 255, 0))
    fill_rect(buf, 0, HEIGHT - 3, 3, 3, (0, 0, 255))
    fill_rect(buf, WIDTH - 3, HEIGHT - 3, 3, 3, (255, 255, 255))
    if on:
        cx, cy = WIDTH // 2, HEIGHT // 2
        for x in range(WIDTH):
            put(buf, x, cy, 255, 0, 255)
        for y in range(HEIGHT):
            put(buf, cx, y, 255, 0, 255)


def sect_countdown(buf, u, t, idx):
    """90-100%: 3-2-1 visszaszamlalas. Az also sav kepkockankent vall oldalt:
    ha a villogas nem egyenletes, kepkockak vesznek el az uton."""
    n = 3 - min(int(u * 3), 2)
    draw_digit(buf, str(n), WIDTH // 2, HEIGHT // 2 - 1, 4, (255, 255, 255))
    if idx % 2 == 0:
        fill_rect(buf, 0, HEIGHT - 2, WIDTH // 2, 2, (0, 255, 0))
    else:
        fill_rect(buf, WIDTH // 2, HEIGHT - 2, WIDTH // 2, 2, (255, 0, 0))


SECTIONS = [
    (0.0000, 0.1667, sect_color_sweep),
    (0.1667, 0.3333, sect_line_scan),
    (0.3333, 0.5333, sect_checker),
    (0.5333, 0.7333, sect_plasma),
    (0.7333, 0.9000, sect_markers),
    (0.9000, 1.0001, sect_countdown),
]


def render(idx, t, duration_s):
    """Egy 2496 bajtos kepkocka. idx: kepkocka sorszama, t: ido masodpercben."""
    buf = new_frame()
    p = t / duration_s if duration_s > 0 else 0.0
    for start, end, fn in SECTIONS:
        if start <= p < end:
            u = (p - start) / (end - start)
            fn(buf, u, t, idx)
            break
    return buf


# -------------------------------------------------------------------
# .q4x konteiner
# -------------------------------------------------------------------

def build_qp4(name):
    """Minimalis QP4 metaadat-blokk. A backend parser atugorja, de a
    q4x_converter.py hibat dob, ha a merete 0."""
    src = (
        "-- __animeditor__\n"
        "meta({\n"
        'audio="",\n'
        'team="",\n'
        'title="' + name + '",\n'
        "year=2026})\n"
        "\n"
        'beginclip(' + str(WIDTH) + ',' + str(HEIGHT) + ',"main")\n'
        "endclip()\n"
    )
    return zlib.compress(src.encode("utf-8"), 9)


def build_q4x(name, fps, duration_s):
    frame_count = int(round(fps * duration_s))
    frame_ms = int(round(1000.0 / fps))
    total_ms = frame_count * frame_ms

    parts = [
        b"qpr v1\n",
        name.encode("utf-8") + b"\n",
        b"\n",                                   # nincs hang
        str(total_ms).encode("ascii") + b"\n",
    ]
    for i in range(frame_count):
        t = i / float(fps)
        parts.append(bytes(render(i, t, duration_s)))
        parts.append(struct.pack(">I", frame_ms))

    qpr = b"".join(parts)
    qprz = zlib.compress(qpr, 9)
    qp4 = build_qp4(name)

    out = bytearray()
    out += b"Q4X1"
    out += struct.pack(">H", WIDTH)
    out += struct.pack(">H", HEIGHT)
    out += struct.pack(">I", len(qp4))
    out += qp4
    out += struct.pack(">I", len(qprz))
    out += qprz
    out += struct.pack(">I", 0)                  # audio size = 0
    return bytes(out), frame_count, frame_ms, total_ms


# -------------------------------------------------------------------
# Onellenorzes a backend sajat parserevel
# -------------------------------------------------------------------

def self_check(data, frame_count, frame_ms, total_ms):
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    parser_path = os.path.normpath(
        os.path.join(here, "..", "backend", "app", "parser.py")
    )
    if not os.path.exists(parser_path):
        print("  [!] backend/app/parser.py nem talalhato, onellenorzes kihagyva")
        return True

    spec = importlib.util.spec_from_file_location("q4x_parser_check", parser_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    parsed = mod.parse_q4x_data(data)
    frames = parsed["frames"]
    state = {"ok": True}

    def check(label, got, want):
        good = got == want
        state["ok"] = state["ok"] and good
        print("  %-4s %-24s %s (vart: %s)"
              % ("OK" if good else "HIBA", label, got, want))

    check("kepkockak szama", len(frames), frame_count)
    check("teljes hossz (ms)", parsed["duration_ms"], total_ms)
    check("kepkocka-idotartamok", sorted(set(d for _, d in frames)), [frame_ms])
    check("kepkocka merete", sorted(set(len(p) for p, _ in frames)), [FRAME_PIXEL_SIZE])
    check("hang", parsed["audio_bytes"], None)
    return state["ok"]


def main():
    ap = argparse.ArgumentParser(description="Teszt .q4x animacio generalasa.")
    ap.add_argument("--fps", type=float, default=25.0,
                    help="fix kepkockasebesseg (alap: 25)")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="hossz masodpercben (alap: 30)")
    ap.add_argument("--name", default=None, help="animacio neve a QPR fejlecben")
    ap.add_argument("--out", default=None, help="kimeneti .q4x fajl")
    args = ap.parse_args()

    if args.fps <= 0 or args.duration <= 0:
        ap.error("--fps es --duration is pozitiv kell legyen")

    fps_label = ("%g" % args.fps).replace(".", "_")
    name = args.name or ("Teszt %gfps" % args.fps)
    out = args.out or ("test_%sfps_%gs.q4x" % (fps_label, args.duration))

    print("Generalas: %g fps, %g s, %dx%d ..." % (args.fps, args.duration, WIDTH, HEIGHT))
    data, frame_count, frame_ms, total_ms = build_q4x(name, args.fps, args.duration)

    with open(out, "wb") as f:
        f.write(data)

    real_fps = 1000.0 / frame_ms
    print("Kesz: %s" % out)
    print("  kepkockak      : %d" % frame_count)
    print("  kepkocka ideje : %d ms (%.2f fps)" % (frame_ms, real_fps))
    print("  teljes hossz   : %d ms" % total_ms)
    print("  fajlmeret      : %.1f KB (tomoritetlen QPR: %.1f MB)"
          % (len(data) / 1024.0,
             frame_count * (FRAME_PIXEL_SIZE + 4) / 1048576.0))
    print("  WebSocket savszelesseg 48x96-on: %.0f KB/s (%.2f Mbps)"
          % (13824 * real_fps / 1024.0, 13824 * real_fps * 8 / 1e6))
    print("Onellenorzes a backend parserevel:")
    if not self_check(data, frame_count, frame_ms, total_ms):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
