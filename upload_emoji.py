"""
Upload icon PNGs as custom emoji to the Discord server.

Usage:
  python upload_emoji.py

Requires DISCORD_TOKEN and a guild ID. Uploads all game/feature icons
as custom emoji named atlas_<icon_name> (e.g., atlas_nfl, atlas_blackjack).

After upload, prints a Python dict mapping icon names to emoji IDs
that can be pasted into the bot code.
"""
import asyncio
import base64
import os
import io

from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = 1480725092328800279  # TSL server

# Icons to upload as custom emoji (name → file path)
BASE = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(BASE, "icons")

EMOJI_ICONS = {
    # Sports
    "atlas_nfl": "nfl.png",
    "atlas_basketball": "basketball.png",
    "atlas_nhl": "nhl.png",
    "atlas_mlb": "mlb.png",
    "atlas_ufc": "ufc.png",
    "atlas_soccer": "soccer.png",
    # Casino games
    "atlas_blackjack": "blackjack.png",
    "atlas_slots": "slots.png",
    "atlas_crash": "crash.png",
    "atlas_coinflip": "coinflip.png",
    "atlas_scratch": "scratch.png",
    # Features
    "atlas_sportsbook": "sportsbook.png",
    "atlas_predictions": "predictions.png",
    "atlas_wallet": "wallet.png",
    "atlas_leaderboard": "leaderboard.png",
    "atlas_parlay": "parlay.png",
}


def prepare_image(path: str) -> str:
    """Resize to 128x128 and return base64 data URI for Discord API."""
    img = Image.open(path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img = img.resize((128, 128), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


async def main():
    if not DISCORD_TOKEN:
        print("ERROR: Set DISCORD_TOKEN environment variable")
        return

    import aiohttp

    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }

    url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/emojis"

    # First, get existing emoji to avoid duplicates
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            existing = await resp.json()
            existing_names = {e["name"] for e in existing}
            print(f"Server has {len(existing)} existing emoji")

        results = {}

        for emoji_name, filename in EMOJI_ICONS.items():
            if emoji_name in existing_names:
                # Find existing ID
                for e in existing:
                    if e["name"] == emoji_name:
                        results[emoji_name] = e["id"]
                        print(f"  [EXISTS] {emoji_name} (ID: {e['id']})")
                        break
                continue

            path = os.path.join(ICONS_DIR, filename)
            if not os.path.exists(path):
                print(f"  [SKIP] {emoji_name}: {filename} not found")
                continue

            image_data = prepare_image(path)
            payload = {"name": emoji_name, "image": image_data}

            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    results[emoji_name] = data["id"]
                    print(f"  [OK] {emoji_name} (ID: {data['id']})")
                else:
                    error = await resp.text()
                    print(f"  [ERROR] {emoji_name}: {resp.status} {error}")

            # Rate limit safety
            await asyncio.sleep(1)

    # Print the mapping dict for bot code
    print("\n# ── Paste this into your bot code ──────────────────────────")
    print("ATLAS_EMOJI = {")
    for name, eid in sorted(results.items()):
        short = name.replace("atlas_", "")
        print(f'    "{short}": discord.PartialEmoji(name="{name}", id={eid}),')
    print("}")


if __name__ == "__main__":
    asyncio.run(main())
