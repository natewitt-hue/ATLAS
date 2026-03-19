"""Shared branding constants for ATLAS modules."""

# TODO: Host on a permanent URL (e.g. GitHub raw, S3, or Imgur).
# This signed Discord CDN link will expire and break embed icons.
ATLAS_ICON_URL = (
    "https://cdn.discordapp.com/attachments/977007320259244055/"
    "1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3"
    "&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
)

# Re-export brand colors from the canonical source.
from atlas_colors import AtlasColors  # noqa: E402

ATLAS_GOLD = AtlasColors.TSL_GOLD
ATLAS_DARK = AtlasColors.TSL_DARK
ATLAS_BLUE = AtlasColors.TSL_BLUE
