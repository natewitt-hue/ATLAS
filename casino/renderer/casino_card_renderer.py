"""
card_renderer.py — TSL Casino Card Renderer
─────────────────────────────────────────────────────────────────────────────
Asset-based rendering. Expects the following directory structure
relative to this file:

  casino/renderer/
  ├── card_renderer.py          ← this file
  ├── fonts/
  │   ├── Montserrat-Bold.ttf
  │   └── Montserrat-Regular.ttf
  ├── cards/
  │   ├── SA.png  SJ.png  SQ.png  SK.png  S2.png … S10.png
  │   ├── HA.png  HJ.png  …
  │   ├── DA.png  DJ.png  …
  │   ├── CA.png  CJ.png  …
  │   └── back.png              ← TSL custom card back
  └── card_renderer.py

Card filenames follow the pattern  {SUIT}{VALUE}.png
  Suit codes:  S=Spades  H=Hearts  D=Diamonds  C=Clubs
  Value codes: A 2 3 4 5 6 7 8 9 10 J Q K

Fell table is still drawn programmatically.
All 52 card faces + back are loaded and cached at startup via warm_cache().
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import io
import os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ── Paths ─────────────────────────────────────────────────────────────────────
_DIR         = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR   = os.path.join(_DIR, "fonts")
_CARDS_DIR   = os.path.join(_DIR, "cards")
_FONT_BOLD   = os.path.join(_FONTS_DIR, "Montserrat-Bold.ttf")
_FONT_REG    = os.path.join(_FONTS_DIR, "Montserrat-Regular.ttf")
_BACK_PATH   = os.path.join(_CARDS_DIR, "back.png")

# Fallback suit font for slot/crash/scratch renderers (unicode glyphs)
_SUIT_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

# ── Deck definition ───────────────────────────────────────────────────────────
SUITS  = ["♠", "♥", "♦", "♣"]
VALUES = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

_SUIT_CODE  = {"♠": "S", "♥": "H", "♦": "D", "♣": "C"}
_VALUE_CODE = {v: v for v in VALUES}   # identity — filenames already match

# ── TSL color palette ─────────────────────────────────────────────────────────
GOLD       = (212, 175, 55)
GOLD_LIGHT = (255, 218, 80)
GOLD_DIM   = (140, 115, 36)
SILVER     = (220, 220, 230)
FELT_COLOR = (13,  52,  26)
FELT_DARK  = (7,   30,  14)
BLACK_BG   = (22,  22,  28)

# ── Card render size ──────────────────────────────────────────────────────────
# Sized so cards are large enough to read on mobile Discord embeds.
# 160px wide fits 4 cards comfortably; 5 cards with tighter gap still works.
CARD_W   = 160
CARD_H   = int(CARD_W * 1024 / 741)   # maintain back.png aspect ratio → ~221px
CARD_GAP = 12

# ── Table dimensions ──────────────────────────────────────────────────────────
TABLE_W  = 920
BAR_H    = 44

# Layout constants
_LABEL_H      = 24
_SCORE_H      = 20
_SCORE_GAP    = 6
_CARDS_GAP    = 10
_MID_GAP      = 60    # space between dealer cards bottom and player label
_TOP_PAD      = 36
_BOT_PAD      = 34

# Derived Y positions
DEALER_LABEL_Y   = _TOP_PAD
DEALER_SCORE_Y   = DEALER_LABEL_Y + _LABEL_H + _SCORE_GAP
DEALER_CARDS_Y   = DEALER_SCORE_Y + _SCORE_H + _CARDS_GAP
DEALER_CARDS_BOT = DEALER_CARDS_Y + CARD_H

PLAYER_LABEL_Y   = DEALER_CARDS_BOT + _MID_GAP
PLAYER_SCORE_Y   = PLAYER_LABEL_Y + _LABEL_H + _SCORE_GAP
PLAYER_CARDS_Y   = PLAYER_SCORE_Y + _SCORE_H + _CARDS_GAP

STATUS_Y         = DEALER_CARDS_BOT + _MID_GAP // 2 - 15

TABLE_H = PLAYER_CARDS_Y + CARD_H + _BOT_PAD + BAR_H


# ─────────────────────────────────────────────────────────────────────────────
#  FONT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fnt(size: int, bold: bool = False) -> ImageFont.ImageFont:
    path = _FONT_BOLD if bold else _FONT_REG
    try:
        return ImageFont.truetype(path, size)
    except (IOError, OSError):
        pass
    for fb in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]:
        try:
            return ImageFont.truetype(fb, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _suit_fnt(size: int) -> ImageFont.ImageFont:
    for path in _SUIT_FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return _fnt(size)


# ─────────────────────────────────────────────────────────────────────────────
#  CARD ASSET CACHE
# ─────────────────────────────────────────────────────────────────────────────

_card_cache: dict[str, Image.Image] = {}


def get_card_image(value: str, suit: str) -> Image.Image:
    """Load, resize and cache a card face asset."""
    key = f"{value}_{suit}"
    if key not in _card_cache:
        fname = f"{_SUIT_CODE[suit]}{_VALUE_CODE[value]}.png"
        path  = os.path.join(_CARDS_DIR, fname)
        img   = Image.open(path).convert("RGBA")
        _card_cache[key] = img.resize((CARD_W, CARD_H), Image.LANCZOS)
    return _card_cache[key]


def get_card_back_image() -> Image.Image:
    """Load, resize and cache the TSL card back."""
    if "BACK" not in _card_cache:
        img = Image.open(_BACK_PATH).convert("RGBA")
        _card_cache["BACK"] = img.resize((CARD_W, CARD_H), Image.LANCZOS)
    return _card_cache["BACK"]


def warm_cache() -> None:
    """Pre-load all 52 card faces + back into memory at startup."""
    for suit in SUITS:
        for value in VALUES:
            try:
                get_card_image(value, suit)
            except FileNotFoundError:
                pass   # missing asset — will fall back at render time
    try:
        get_card_back_image()
    except FileNotFoundError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  SHADOW + ROTATION
# ─────────────────────────────────────────────────────────────────────────────

def _paste_with_shadow(
    table:  Image.Image,
    card:   Image.Image,
    x:      int,
    y:      int,
    offset: int = 5,
    blur:   int = 6,
) -> Image.Image:
    shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    sf     = Image.new("RGBA", card.size, (0, 0, 0, 210))
    mask   = card.split()[3] if card.mode == "RGBA" else None
    if mask:
        shadow.paste(sf, mask=mask)
    else:
        shadow = sf.copy()
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))

    base = table.convert("RGBA")
    base.paste(shadow, (x + offset, y + offset), shadow)
    base.paste(card, (x, y), card if card.mode == "RGBA" else None)
    return base.convert("RGB")


def _paste_card_rotated(
    table: Image.Image,
    card:  Image.Image,
    x:     int,
    y:     int,
    angle: float,
) -> Image.Image:
    if abs(angle) < 0.01:
        return _paste_with_shadow(table, card, x, y)
    pad = 22
    exp = Image.new("RGBA", (card.width + pad*2, card.height + pad*2), (0, 0, 0, 0))
    exp.paste(card, (pad, pad), card if card.mode == "RGBA" else None)
    rot = exp.rotate(angle, resample=Image.BICUBIC, expand=False)
    return _paste_with_shadow(table, rot, x - pad, y - pad)


def _paste_hand(
    table:       Image.Image,
    hand:        list[tuple[str, str]],
    y_top:       int,
    hide_second: bool = False,
    seed:        int  = 0,
) -> Image.Image:
    """Paste a hand centered on the table with slight rotation variance."""
    n       = len(hand)
    gap     = CARD_GAP
    # Dynamic overlap for 6+ cards — compress gap so hand fits within table
    total_w = n * CARD_W + (n - 1) * gap
    if total_w > TABLE_W - 40:
        gap     = max(-(CARD_W // 3), (TABLE_W - 40 - n * CARD_W) // max(n - 1, 1))
        total_w = n * CARD_W + (n - 1) * gap
    x_start = (TABLE_W - total_w) // 2
    rng     = random.Random(seed)

    for i, (v, s) in enumerate(hand):
        x     = x_start + i * (CARD_W + gap)
        angle = rng.uniform(-2.0, 2.0)
        xo    = rng.randint(-3, 3)
        yo    = rng.randint(-2, 2)
        card  = (get_card_back_image() if (hide_second and i == 1)
                 else get_card_image(v, s))
        table = _paste_card_rotated(table, card, x + xo, y_top + yo, angle)
    return table


# ─────────────────────────────────────────────────────────────────────────────
#  FELT TABLE BASE
# ─────────────────────────────────────────────────────────────────────────────

def _draw_felt_base() -> Image.Image:
    img  = Image.new("RGB", (TABLE_W, TABLE_H), FELT_COLOR)
    draw = ImageDraw.Draw(img)

    # Vertical gradient
    for y in range(TABLE_H):
        t = y / TABLE_H
        r = int(FELT_COLOR[0] + (FELT_DARK[0] - FELT_COLOR[0]) * t * 0.5)
        g = int(FELT_COLOR[1] + (FELT_DARK[1] - FELT_COLOR[1]) * t * 0.5)
        b = int(FELT_COLOR[2] + (FELT_DARK[2] - FELT_COLOR[2]) * t * 0.5)
        draw.line([(0, y), (TABLE_W, y)], fill=(r, g, b))

    # Oval border — contained within the felt above the info bar
    felt_h = TABLE_H - BAR_H
    m      = 26
    draw.ellipse([m, m, TABLE_W - m, felt_h - m], outline=GOLD, width=3)
    draw.ellipse([m+6, m+6, TABLE_W-m-6, felt_h-m-6], outline=GOLD_DIM, width=1)

    # TSL watermark
    wf  = _fnt(120, bold=True)
    wm  = Image.new("RGBA", (TABLE_W, TABLE_H), (0, 0, 0, 0))
    wd  = ImageDraw.Draw(wm)
    b   = wd.textbbox((0, 0), "TSL", font=wf)
    tw  = b[2] - b[0]
    th  = b[3] - b[1]
    wd.text(
        ((TABLE_W - tw) // 2 - b[0], (felt_h - th) // 2 - b[1]),
        "TSL", font=wf,
        fill=(FELT_DARK[0]+12, FELT_DARK[1]+12, FELT_DARK[2]+12, 255)
    )
    return Image.alpha_composite(img.convert("RGBA"), wm).convert("RGB")


# ─────────────────────────────────────────────────────────────────────────────
#  BLACKJACK TABLE RENDERER
# ─────────────────────────────────────────────────────────────────────────────

def render_blackjack_table(
    dealer_hand:  list[tuple[str, str]],
    player_hand:  list[tuple[str, str]],
    dealer_score: int | str,
    player_score: int | str,
    hide_dealer:  bool = True,
    status:       str  = "",
    wager:        int  = 0,
    balance:      int  = 0,
) -> io.BytesIO:
    """
    Render a full blackjack table image and return it as a PNG BytesIO.

    Layout (top → bottom):
      DEALER label + score
      Dealer cards  (rotated, drop shadow; second card face-down if hide_dealer)
      Status message (centered in mid-gap)
      PLAYER label + score
      Player cards  (rotated, drop shadow)
      Info bar      (wager left, balance right)
    """
    table = _draw_felt_base()
    draw  = ImageDraw.Draw(table)

    lf  = _fnt(22, bold=True)    # label font (DEALER / PLAYER)
    sf  = _fnt(17)               # score font
    stf = _fnt(34, bold=True)    # status font (Win! / Bust! etc.)
    inf = _fnt(16)               # info bar font

    def _cx(text: str, font) -> int:
        """Return X so text is horizontally centered."""
        b = draw.textbbox((0, 0), text, font=font)
        return (TABLE_W - (b[2] - b[0])) // 2

    # ── Dealer ────────────────────────────────────────────────────────────
    draw.text((_cx("DEALER", lf), DEALER_LABEL_Y), "DEALER", font=lf, fill=GOLD)
    sc_txt = f"Score: {dealer_score}"
    draw.text((_cx(sc_txt, sf), DEALER_SCORE_Y), sc_txt, font=sf, fill=SILVER)
    table = _paste_hand(table, dealer_hand, DEALER_CARDS_Y,
                        hide_second=hide_dealer, seed=42)

    # ── Status ────────────────────────────────────────────────────────────
    if status:
        draw = ImageDraw.Draw(table)
        status_colors = {
            "Blackjack! 🎉":    GOLD_LIGHT,
            "Win! ✅":          (100, 245, 100),
            "Bust! ❌":         (255, 75,  75),
            "Push 🔁":          (200, 200, 200),
            "Dealer Busts! 🎉": (100, 245, 100),
            "Loss ❌":          (255, 75,  75),
        }
        sc = status_colors.get(status, (200, 200, 200))
        draw.text((_cx(status, stf), STATUS_Y), status, font=stf, fill=sc)

    # ── Player ────────────────────────────────────────────────────────────
    draw = ImageDraw.Draw(table)
    draw.text((_cx("PLAYER", lf), PLAYER_LABEL_Y), "PLAYER", font=lf, fill=GOLD)
    ps_txt = f"Score: {player_score}"
    draw.text((_cx(ps_txt, sf), PLAYER_SCORE_Y), ps_txt, font=sf, fill=SILVER)
    table = _paste_hand(table, player_hand, PLAYER_CARDS_Y, seed=99)

    # ── Info bar ──────────────────────────────────────────────────────────
    draw  = ImageDraw.Draw(table)
    bar_y = TABLE_H - BAR_H
    draw.rectangle([(0, bar_y), (TABLE_W, TABLE_H)], fill=(5, 7, 9))
    draw.line([(0, bar_y), (TABLE_W, bar_y)], fill=GOLD_DIM, width=1)
    draw.text((18, bar_y + 13), f"Wager: {wager:,} TSL Bucks", font=inf, fill=GOLD)
    bt  = f"Balance: {balance:,} TSL Bucks"
    bb  = draw.textbbox((0, 0), bt, font=inf)
    draw.text((TABLE_W - (bb[2]-bb[0]) - 18, bar_y + 13), bt, font=inf, fill=SILVER)

    buf = io.BytesIO()
    table.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────────────
#  SLOT MACHINE RENDERER  (unchanged — no card assets involved)
# ─────────────────────────────────────────────────────────────────────────────

SLOT_SYMBOLS = {
    "shield":   ("🛡", GOLD_LIGHT),
    "crown":    ("♛",  GOLD),
    "trophy":   ("🏆", (255, 200, 50)),
    "football": ("🏈", (200, 120, 60)),
    "star":     ("★",  (220, 220, 100)),
    "coin":     ("●",  (180, 150, 50)),
}
SLOT_W = 420
SLOT_H = 220


def render_slot_result(
    reels:      list[str],
    revealed:   int = 3,
    wager:      int = 0,
    payout:     int = 0,
    balance:    int = 0,
    result_msg: str = "",
) -> io.BytesIO:
    img  = Image.new("RGB", (SLOT_W, SLOT_H), BLACK_BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(2,2),(SLOT_W-3,SLOT_H-3)], radius=14, outline=GOLD, width=3)
    draw.rounded_rectangle([(6,6),(SLOT_W-7,SLOT_H-7)], radius=11, outline=GOLD_DIM, width=1)

    tf = _fnt(14, bold=True)
    draw.rectangle([(10,10),(SLOT_W-10,35)], fill=(25,20,5))
    draw.rounded_rectangle([(10,10),(SLOT_W-10,35)], radius=6, outline=GOLD, width=1)
    title = "🎰  TSL SLOTS  🎰"
    tb    = draw.textbbox((0,0), title, font=tf)
    draw.text(((SLOT_W-(tb[2]-tb[0]))//2, 14), title, font=tf, fill=GOLD_LIGHT)

    reel_size    = 80
    reel_gap     = 15
    total_rw     = 3*reel_size + 2*reel_gap
    reel_x_start = (SLOT_W-total_rw)//2
    reel_y       = 50
    sym_font     = _suit_fnt(38)

    for i in range(3):
        rx = reel_x_start + i*(reel_size+reel_gap)
        draw.rounded_rectangle(
            [(rx,reel_y),(rx+reel_size,reel_y+reel_size)],
            radius=8, fill=(28,28,32),
            outline=GOLD if i<revealed else (60,60,60), width=2
        )
        if i < revealed:
            sc, col = SLOT_SYMBOLS.get(reels[i], ("?", SILVER))
            sb = draw.textbbox((0,0), sc, font=sym_font)
            sw = sb[2]-sb[0]; sh = sb[3]-sb[1]
            draw.text((rx+(reel_size-sw)//2-sb[0], reel_y+(reel_size-sh)//2-sb[1]),
                      sc, font=sym_font, fill=col)
        else:
            qf = _fnt(40, bold=True)
            qb = draw.textbbox((0,0),"?",font=qf)
            draw.text((rx+(reel_size-(qb[2]-qb[0]))//2-qb[0],
                       reel_y+(reel_size-(qb[3]-qb[1]))//2-qb[1]),
                      "?", font=qf, fill=(80,80,80))

    if result_msg:
        rf = _fnt(16, bold=True)
        rc = GOLD_LIGHT if payout > 0 else SILVER
        rb = draw.textbbox((0,0), result_msg, font=rf)
        draw.text(((SLOT_W-(rb[2]-rb[0]))//2, reel_y+reel_size+10),
                  result_msg, font=rf, fill=rc)

    inf = _fnt(11)
    draw.rectangle([(10,SLOT_H-30),(SLOT_W-10,SLOT_H-10)], fill=(20,15,5))
    draw.rounded_rectangle([(10,SLOT_H-30),(SLOT_W-10,SLOT_H-10)],
                           radius=4, outline=GOLD_DIM, width=1)
    draw.text((16,SLOT_H-24),
              f"Wager: {wager:,}  |  Win: {payout:,}  |  Balance: {balance:,}",
              font=inf, fill=GOLD)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────────────
#  CRASH CHART RENDERER
# ─────────────────────────────────────────────────────────────────────────────

CRASH_W = 500
CRASH_H = 300


def render_crash_chart(
    current_mult:  float,
    crashed:       bool            = False,
    history:       list[float] | None = None,
    players_in:    int             = 0,
    total_wagered: int             = 0,
) -> io.BytesIO:
    img  = Image.new("RGB", (CRASH_W, CRASH_H), (10, 10, 15))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(2,2),(CRASH_W-3,CRASH_H-3)], radius=10, outline=GOLD, width=2)

    gc = (28, 28, 38)
    for gx in range(50, CRASH_W, 50):
        draw.line([(gx,10),(gx,CRASH_H-40)], fill=gc)
    for gy in range(10, CRASH_H-40, 40):
        draw.line([(10,gy),(CRASH_W-10,gy)], fill=gc)

    chart_w = CRASH_W - 30
    chart_h = CRASH_H - 60
    max_m   = max(current_mult * 1.3, 2.5)
    points  = []
    for i in range(61):
        t  = i / 60
        m  = 1.0 + (current_mult - 1.0) * (t ** 1.5)
        px = 15 + int(t * chart_w)
        py = (CRASH_H-45) - int(((m-1.0)/(max_m-1.0)) * chart_h)
        py = max(10, min(CRASH_H-45, py))
        points.append((px, py))

    if len(points) > 1:
        color = (255,60,60) if crashed else GOLD
        glow  = (120,30,30) if crashed else (100,80,20)
        draw.line(points, fill=glow, width=5)
        draw.line(points, fill=color, width=2)

    mf = _fnt(48, bold=True)
    if crashed:
        ms = f"CRASHED @ {current_mult:.2f}x"
        mc = (255, 60, 60)
        mf = _fnt(28, bold=True)
    else:
        ms = f"{current_mult:.2f}x"
        mc = GOLD_LIGHT

    mb = draw.textbbox((0,0), ms, font=mf)
    mw = mb[2]-mb[0]
    draw.text(((CRASH_W-mw)//2, CRASH_H//2-40), ms, font=mf, fill=mc)

    if history:
        hf = _fnt(10)
        draw.text((15, CRASH_H-32), "Recent:", font=hf, fill=(100,100,100))
        x = 65
        for h in history[-6:]:
            hc = (255,100,100) if h<2.0 else (GOLD if h<10.0 else GOLD_LIGHT)
            draw.text((x, CRASH_H-32), f"{h:.2f}x", font=hf, fill=hc)
            x += 55

    inf = _fnt(11)
    info = f"👥 {players_in} players  |  💰 {total_wagered:,} TSL Bucks at risk"
    ib = draw.textbbox((0,0), info, font=inf)
    iw = ib[2]-ib[0]
    draw.text(((CRASH_W-iw)//2, CRASH_H-18), info, font=inf, fill=SILVER)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────────────
#  SCRATCH CARD RENDERER
# ─────────────────────────────────────────────────────────────────────────────

SCRATCH_W = 420
SCRATCH_H = 200
TILE_W    = 100
TILE_H    = 80


def render_scratch_card(
    tiles:    list[int],
    revealed: int  = 0,
    is_match: bool = False,
) -> io.BytesIO:
    img  = Image.new("RGB", (SCRATCH_W, SCRATCH_H), (20, 15, 5))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(2,2),(SCRATCH_W-3,SCRATCH_H-3)], radius=12, outline=GOLD, width=3)

    hf  = _fnt(13, bold=True)
    draw.rectangle([(10,10),(SCRATCH_W-10,35)], fill=(35,25,5))
    draw.rounded_rectangle([(10,10),(SCRATCH_W-10,35)], radius=5, outline=GOLD, width=1)
    hdr = "🎟️  TSL DAILY SCRATCH  🎟️"
    hb  = draw.textbbox((0,0), hdr, font=hf)
    hw  = hb[2]-hb[0]
    draw.text(((SCRATCH_W-hw)//2, 14), hdr, font=hf, fill=GOLD_LIGHT)

    tile_gap     = 20
    total_tw     = 3*TILE_W + 2*tile_gap
    tile_x_start = (SCRATCH_W-total_tw)//2
    tile_y       = 48
    tf           = _fnt(22, bold=True)
    lf           = _suit_fnt(28)

    for i in range(3):
        tx = tile_x_start + i*(TILE_W+tile_gap)
        if i < revealed:
            bg = (35,28,8) if not is_match else (40,32,5)
            oc = GOLD_LIGHT if is_match else GOLD
            draw.rounded_rectangle([(tx,tile_y),(tx+TILE_W,tile_y+TILE_H)],
                                   radius=8, fill=bg, outline=oc, width=2)
            vs = f"{tiles[i]:,}"
            vb = draw.textbbox((0,0), vs, font=tf)
            vw = vb[2]-vb[0]; vh = vb[3]-vb[1]
            draw.text((tx+(TILE_W-vw)//2-vb[0], tile_y+(TILE_H-vh)//2-vb[1]),
                      vs, font=tf, fill=GOLD_LIGHT if is_match else SILVER)
            bf = _fnt(9)
            draw.text((tx+TILE_W//2-12, tile_y+TILE_H-16), "BUCKS", font=bf, fill=GOLD_DIM)
        else:
            draw.rounded_rectangle([(tx,tile_y),(tx+TILE_W,tile_y+TILE_H)],
                                   radius=8, fill=(40,32,8), outline=GOLD_DIM, width=2)
            for hatch in range(0, TILE_W, 10):
                draw.line([(tx+hatch,tile_y),(tx,tile_y+hatch)], fill=(50,40,10), width=1)
            lb = draw.textbbox((0,0),"🔒",font=lf)
            lw = lb[2]-lb[0]; lh = lb[3]-lb[1]
            draw.text((tx+(TILE_W-lw)//2-lb[0], tile_y+(TILE_H-lh)//2-lb[1]),
                      "🔒", font=lf, fill=(80,64,16))

    if is_match and revealed == 3:
        mf = _fnt(14, bold=True)
        ms = "🏆 TRIPLE MATCH — 3x BONUS! 🏆"
        mb = draw.textbbox((0,0), ms, font=mf)
        mw = mb[2]-mb[0]
        draw.text(((SCRATCH_W-mw)//2, tile_y+TILE_H+8), ms, font=mf, fill=GOLD_LIGHT)
    elif revealed < 3:
        hintf = _fnt(11)
        remaining = 3 - revealed
        hs = f"Tap 'Scratch' to reveal ({remaining} tile{'s' if remaining>1 else ''} left)"
        hb = draw.textbbox((0,0), hs, font=hintf)
        hw = hb[2]-hb[0]
        draw.text(((SCRATCH_W-hw)//2, tile_y+TILE_H+12), hs, font=hintf, fill=(150,150,150))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf