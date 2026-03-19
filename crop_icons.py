"""Crop individual icons from grid images - center-crop approach, no labels."""
from PIL import Image
import os

ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ACHIEVEMENTS_DIR = os.path.join(ICONS_DIR, "achievements")
os.makedirs(ACHIEVEMENTS_DIR, exist_ok=True)

TARGET = 256  # output size


def center_crop(img, cx, cy, size, name, out_dir):
    """Crop a square centered on (cx, cy), resize, save."""
    half = size // 2
    box = (cx - half, cy - half, cx + half, cy + half)
    cell = img.crop(box)
    cell = cell.resize((TARGET, TARGET), Image.LANCZOS)
    path = os.path.join(out_dir, f"{name}.png")
    cell.save(path, "PNG")


# ═══════════════════════════════════════════════════════════════════════════
# GRID 1: 4x4 Game Icons (1024x1024)
# ═══════════════════════════════════════════════════════════════════════════
# Grid has ~10px outer gold border.
# Usable area: ~10 to ~1014 = 1004px
# 4 cells per axis: ~251px per cell
# Each cell = icon (~200px square) + label text (~30px) below
# Icon is vertically centered in upper ~200px of each cell

# Cell width: (1014 - 10) / 4 = 251
# Column centers (x): 10 + 251*0.5 = 135, +251 = 386, +251 = 637, +251 = 888
# But the labels push icons UP within each cell.
# Icon center y offsets: icon occupies top ~200px of ~251px cell
# So icon center y = cell_top + 100

# Row top edges (after border): 10, 261, 512, 763
# Icon center y: 10+100=110, 261+100=361, 512+100=612, 763+100=863

img1 = Image.open(os.path.join(ICONS_DIR, "grid_game.png"))

# These centers were derived from pixel analysis above
# Blackjack border: y=17-215, so center_y = 116
# Sportsbook golden border: y=288 -> icon ~288-488 -> center_y = 388
# Adjusting empirically based on the pixel scans:
game_cx = [122, 370, 617, 865]
game_cy = [116, 392, 635, 868]
ICON_CROP = 180  # tighter crop to avoid label bleed

GAME_GRID = [
    ["blackjack", "slots", "crash", "coinflip"],
    ["predictions", "sportsbook", "nfl", "basketball"],
    ["mlb", "nhl", "ufc", "soccer"],
    ["hot_streak", "cold_streak", "jackpot", "player_profiles"],
]

print("=== Game Icons ===")
for ri, names in enumerate(GAME_GRID):
    for ci, name in enumerate(names):
        center_crop(img1, game_cx[ci], game_cy[ri], ICON_CROP, name, ICONS_DIR)
        print(f"  {name}.png")

# ═══════════════════════════════════════════════════════════════════════════
# GRID 2: 5x4 Achievement Icons (1024x1024)
# ═══════════════════════════════════════════════════════════════════════════
# 5 columns, 4 rows
# Cell width: ~1004/5 = ~200, Cell height: ~1004/4 = ~251
# Icons are ~150px square in upper portion of each cell

img2 = Image.open(os.path.join(ICONS_DIR, "grid_achievements.png"))

ach_cx = [110, 310, 510, 710, 912]
ach_cy = [116, 392, 635, 868]
ACH_CROP = 155  # achievement icons are smaller in the 5-col grid

ACHIEVEMENT_GRID = [
    ["first_timer", "regular", "high_roller", "whale", "lucky_7"],
    ["perfect_hand", "rocketman", "moon_shot", "nerves_of_steel", "comeback_king"],
    ["all_rounder", "dedicated", "iron_will", "jackpot_club", "grand_slam"],
    ["big_spender", "high_society", "legend", "challenger", "crowd_player"],
]

print("\n=== Achievement Icons ===")
for ri, names in enumerate(ACHIEVEMENT_GRID):
    for ci, name in enumerate(names):
        center_crop(img2, ach_cx[ci], ach_cy[ri], ACH_CROP, name, ACHIEVEMENTS_DIR)
        print(f"  {name}.png")

# ═══════════════════════════════════════════════════════════════════════════
# GRID 3: 4x3 Slot Machine Symbols (1024x1024)
# ═══════════════════════════════════════════════════════════════════════════
# Title banner "CASINO ROYALE SLOT SYMBOLS v1.0" takes ~65px at top.
# Then 3 rows of 4 icons each, with labels below.
# Footer watermark at bottom ~25px.
# Usable height for icons: ~70 to ~980 = 910px for 3 rows = ~303px per row
# Usable width: ~10 to ~1014 = 1004px for 4 cols = ~251px per col

SLOT_ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "casino", "renderer", "slot_icons")
os.makedirs(SLOT_ICONS_DIR, exist_ok=True)

slot_grid_path = os.path.join(ICONS_DIR, "grid_slots.png")
if os.path.exists(slot_grid_path):
    img3 = Image.open(slot_grid_path)
    print("\n=== Slot Icons ===")

    # 4 columns, same x-centers as game grid
    slot_cx = [135, 383, 631, 879]
    # 3 rows, shifted down due to title banner (~65px)
    # Row 1 icons: ~80 to ~280, center ~180
    # Row 2 icons: ~340 to ~540, center ~440
    # Row 3 icons: ~600 to ~800, center ~700
    slot_cy = [190, 480, 760]
    SLOT_CROP = 185  # icon squares within each cell

    # Only crop the 7 we need (skip R2C4=dice, R3=variants)
    SLOT_MAP = [
        # Row 1: Wild, Lucky 7 (star), Gold Bars (trophy), Diamond Star (shield)
        (0, 0, "wild"),
        (0, 1, "star"),
        (0, 2, "trophy"),
        (0, 3, "shield"),
        # Row 2: Royal Crown, Bar Symbol (football), Chip Stack (coin)
        (1, 0, "crown"),
        (1, 1, "football"),
        (1, 2, "coin"),
    ]

    for ri, ci, name in SLOT_MAP:
        center_crop(img3, slot_cx[ci], slot_cy[ri], SLOT_CROP, name, SLOT_ICONS_DIR)
        print(f"  {name}.png")
else:
    print("\n[SKIP] grid_slots.png not found")


# ═══════════════════════════════════════════════════════════════════════════
# FEATURE ICONS: Pre-split from zip (4 PNGs, ~520x520 each)
# ═══════════════════════════════════════════════════════════════════════════
# These have label text bleed at top/bottom edges.
# Center-crop to remove labels, resize to 256x256.

INCOMING_DIR = os.path.join(ICONS_DIR, "_incoming", "PineTools.com_files")

FEATURE_MAP = [
    ("row-1-column-1.png", "scratch"),
    ("row-1-column-2.png", "parlay"),
    ("row-2-column-1.png", "wallet"),
    ("row-2-column-2.png", "leaderboard"),
]

if os.path.exists(INCOMING_DIR):
    print("\n=== Feature Icons ===")
    for src_name, out_name in FEATURE_MAP:
        src_path = os.path.join(INCOMING_DIR, src_name)
        if os.path.exists(src_path):
            img = Image.open(src_path)
            w, h = img.size
            # Center-crop ~340x340 to aggressively avoid label text
            crop_size = min(w, h) - 180  # ~340 from 520
            cx, cy = w // 2, h // 2 - 10  # shift up slightly (labels are at bottom)
            half = crop_size // 2
            box = (cx - half, cy - half, cx + half, cy + half)
            cell = img.crop(box)
            cell = cell.resize((TARGET, TARGET), Image.LANCZOS)
            out_path = os.path.join(ICONS_DIR, f"{out_name}.png")
            cell.save(out_path, "PNG")
            print(f"  {out_name}.png ({cell.size[0]}x{cell.size[1]})")
        else:
            print(f"  [SKIP] {src_name} not found")
else:
    print("\n[SKIP] _incoming directory not found")


print(f"\nDone - all icons at {TARGET}x{TARGET}")
