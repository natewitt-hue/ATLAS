# Adversarial Review: upload_emoji.py

**Verdict:** LIVE (CLI script — keep but harden)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 127
**Reviewer:** Claude (delegated subagent)
**Total findings:** 6 (1 critical, 2 warnings, 3 observations)

## Summary

One-shot CLI script that uploads icon PNGs as Discord custom emoji. Has `if __name__ == "__main__":` and is run as `python upload_emoji.py`. Not dead, but contains a hardcoded production GUILD_ID, no rate-limit handling beyond a 1-second sleep, and prints the entire ATLAS_EMOJI dict to stdout — a copy-paste workflow that bypasses code review.

## Findings

### CRITICAL #1: Hardcoded production GUILD_ID for TSL server
**Location:** `C:/Users/natew/Desktop/discord_bot/upload_emoji.py:22`
**Confidence:** 0.95
**Risk:** `GUILD_ID = 1480725092328800279  # TSL server` is a hardcoded production guild ID. Anyone running this script with a valid `DISCORD_TOKEN` will modify the production TSL server's emoji set. There is no `--guild-id` arg, no confirmation prompt, no dry-run flag. A developer testing the script accidentally hits production. Worse: a developer with a personal `DISCORD_TOKEN` for a different server can't easily test without source-edit because the GUILD_ID is hardcoded.
**Vulnerability:** No environment override, no confirmation prompt.
**Impact:** Accidental production emoji modification. If the script is rerun against a server that already has these emoji, the existing-detection logic guards against duplicates but doesn't guard against name conflicts (e.g. if `atlas_nfl` already exists with different image). Per CLAUDE.md `DISCORD_GUILD_ID` env var exists for this purpose.
**Fix:** Read `GUILD_ID` from `os.getenv("DISCORD_GUILD_ID", "")`. Add `--dry-run` and `--guild-id` flags. Confirm guild name before posting.

### WARNING #1: No 429 (rate limit) handling
**Location:** `C:/Users/natew/Desktop/discord_bot/upload_emoji.py:105-115`
**Confidence:** 0.85
**Risk:** Discord's emoji upload endpoint has tight per-guild rate limits (50 requests / 1 hour for emoji creation). Script sleeps 1 second between calls but with 16 emoji to upload, that's 16 seconds total — well within burst tolerance. If the script is rerun and most emoji already exist (skipping the API call), no problem. But if the operator runs it multiple times in the same hour (e.g. troubleshooting), they will hit 429 and the script will print `[ERROR]` and continue, never retrying.
**Vulnerability:** No retry-after parsing, no backoff.
**Impact:** Partial uploads on rate limit, which the operator may not notice.
**Fix:** Parse `resp.headers.get("Retry-After")` and respect it.

### WARNING #2: prepare_image() runs blocking PIL inside async function
**Location:** `C:/Users/natew/Desktop/discord_bot/upload_emoji.py:51-61, 102-103`
**Confidence:** 0.7
**Risk:** `prepare_image` opens a PNG, converts to RGBA, resizes, encodes base64 — all CPU/IO blocking. It's called from inside `async def main()` without `asyncio.to_thread`. Per ATLAS focus block: "Blocking calls inside `async` functions ... All blocking I/O must go through `asyncio.to_thread()`." Single-coroutine script, so no functional impact, but the pattern is bad and could leak into a cog if anyone copies it.
**Impact:** Bad pattern.
**Fix:** `image_data = await asyncio.to_thread(prepare_image, path)`.

### OBSERVATION #1: Dead-candidate but live CLI
**Location:** `C:/Users/natew/Desktop/discord_bot/upload_emoji.py:126-127`
**Confidence:** 0.95
**Risk:** Has main entrypoint, undocumented anywhere.
**Fix:** Move to `scripts/` or document in README.

### OBSERVATION #2: Output dict goes to stdout for copy-paste — workflow gap
**Location:** `C:/Users/natew/Desktop/discord_bot/upload_emoji.py:117-123`
**Confidence:** 0.7
**Risk:** Final step is `print("ATLAS_EMOJI = {")` and the operator manually copy-pastes into the bot codebase. This bypasses code review of the IDs and is error-prone (line truncation, copy errors, accidental deletion of comments).
**Impact:** Source of truth is implicit — emoji IDs live in two places (Discord server + bot code) with manual sync.
**Fix:** Write to `assets/atlas_emoji.json` and have the bot load from disk; OR write directly to a `_generated_emoji.py` file that's gitignored.

### OBSERVATION #3: `existing_names` derived from `existing` which may not be a list
**Location:** `C:/Users/natew/Desktop/discord_bot/upload_emoji.py:80-83`
**Confidence:** 0.55
**Risk:** `existing = await resp.json()` — if Discord returns an error response (e.g. 401 for bad token, 404 for unknown guild), `existing` is a dict like `{"message": "Unauthorized"}`, not a list. `{e["name"] for e in existing}` then iterates dict keys producing `{"message"}` and skips the validation entirely. The script proceeds to try POSTing emoji, which all fail with the same error.
**Impact:** Confusing error spam instead of fast-fail.
**Fix:** Check `resp.status == 200` first, raise on failure.

## Cross-cutting Notes

The hardcoded GUILD_ID in this file is duplicated in spirit by other scripts that may also have hardcoded server context. Recommend a project-wide grep for `1480725092328800279` to find every place the production guild ID is baked in. CLAUDE.md says `DISCORD_GUILD_ID` env var defaults to 0 — that should be the single source of truth.
