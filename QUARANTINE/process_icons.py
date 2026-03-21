"""
Auto-trim and resize icons from a staging folder.

Usage:
  1. Drop PNG/JPEG files into icons/_staging/
  2. Name them exactly as they should appear in the project:
     - Game icons:        blackjack.png, slots.png, crash.png, etc.
     - Slot symbols:      wild.png, star.png, trophy.png, shield.png, crown.png, football.png, coin.png
     - Achievement icons: first_timer.png, regular.png, high_roller.png, etc.
  3. Run: python process_icons.py
  4. Icons are auto-trimmed, resized to 256x256, and moved to the correct directory.
"""
from PIL import Image
import os

TARGET = 256
BASE = os.path.dirname(os.path.abspath(__file__))
STAGING = os.path.join(BASE, "icons", "_staging")
ICONS_DIR = os.path.join(BASE, "icons")
ACH_DIR = os.path.join(ICONS_DIR, "achievements")
SLOT_DIR = os.path.join(BASE, "casino", "renderer", "slot_icons")

# Which filenames go where
SLOT_NAMES = {"wild", "star", "trophy", "shield", "crown", "football", "coin"}
ACH_NAMES = {
    "first_timer", "regular", "high_roller", "whale", "lucky_7",
    "perfect_hand", "rocketman", "moon_shot", "nerves_of_steel", "comeback_king",
    "all_rounder", "dedicated", "iron_will", "jackpot_club", "grand_slam",
    "big_spender", "high_society", "legend", "challenger", "crowd_player",
}

os.makedirs(STAGING, exist_ok=True)
os.makedirs(ACH_DIR, exist_ok=True)
os.makedirs(SLOT_DIR, exist_ok=True)


def trim_and_resize(img_path):
    """Trim transparent padding, resize to TARGET square, return Image."""
    img = Image.open(img_path)

    # Convert to RGBA if not already
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # Get bounding box of non-transparent pixels
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    # Make square by padding the shorter side with transparency
    w, h = img.size
    side = max(w, h)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - w) // 2, (side - h) // 2))

    # Resize to target
    square = square.resize((TARGET, TARGET), Image.LANCZOS)
    return square


def main():
    files = [f for f in os.listdir(STAGING)
             if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]

    if not files:
        print(f"No images found in {STAGING}/")
        print("Drop your icon PNGs there and re-run.")
        return

    print(f"Processing {len(files)} icon(s) from _staging/...\n")

    for fname in sorted(files):
        src = os.path.join(STAGING, fname)
        name = os.path.splitext(fname)[0].lower()

        # Determine output directory
        if name in SLOT_NAMES:
            out_dir = SLOT_DIR
            category = "slot"
        elif name in ACH_NAMES:
            out_dir = ACH_DIR
            category = "achievement"
        else:
            out_dir = ICONS_DIR
            category = "game"

        try:
            result = trim_and_resize(src)
            out_path = os.path.join(out_dir, f"{name}.png")
            result.save(out_path, "PNG")

            # Remove from staging after successful processing
            os.remove(src)

            print(f"  [{category:11s}] {name}.png -> {out_path}")
        except Exception as e:
            print(f"  [ERROR] {fname}: {e}")

    print(f"\nDone. All icons at {TARGET}x{TARGET}.")


if __name__ == "__main__":
    main()
