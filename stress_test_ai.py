"""
Stress test for atlas_ai.py — 50 TSL questions across all tiers and modes.
Run: python stress_test_ai.py
"""
import asyncio
import time
import sys
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# Fix Windows console encoding
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(override=True)

import atlas_ai
from atlas_ai import Tier

# ── 50 TSL questions organized by tier and mode ──────────────────────────

QUESTIONS = [
    # ── HAIKU tier (fast, cheap — classification, blurbs) ──
    (Tier.HAIKU, "generate", "Who won Super Bowl 95 in TSL?"),
    (Tier.HAIKU, "generate", "Name three TSL teams that have never won a Super Bowl."),
    (Tier.HAIKU, "generate", "What is the TSL trade deadline rule?"),
    (Tier.HAIKU, "generate", "Summarize the TSL salary cap rules in one sentence."),
    (Tier.HAIKU, "generate", "Which TSL division is the most competitive right now?"),
    (Tier.HAIKU, "generate", "What does devTrait 3 mean in Madden?"),
    (Tier.HAIKU, "generate", "Is tanking allowed in TSL?"),
    (Tier.HAIKU, "generate", "What is a 4th down complaint in TSL?"),
    (Tier.HAIKU, "generate", "Name the NFC East teams in TSL."),
    (Tier.HAIKU, "generate", "What are ability budgets for Superstar X-Factor players?"),

    # ── HAIKU tier with json_mode ──
    (Tier.HAIKU, "json", "Return a JSON object with keys 'team' and 'wins' for the best TSL team this season."),
    (Tier.HAIKU, "json", "Return JSON: {\"question\": \"who is the TSL MVP?\", \"category\": \"awards\"}"),
    (Tier.HAIKU, "json", "Classify this intent as JSON: {\"intent\": \"stats\"|\"trade\"|\"rule\"} for: 'How many passing yards does the Eagles QB have?'"),
    (Tier.HAIKU, "json", "Return JSON with keys 'legal' and 'reason' for: 'Can I trade 3 first round picks for one player?'"),
    (Tier.HAIKU, "json", "Return a JSON array of 3 TSL team names."),

    # ── SONNET tier (balanced — analytics, chat, SQL gen) ──
    (Tier.SONNET, "generate", "Analyze the Eagles roster strengths and weaknesses for a sim league."),
    (Tier.SONNET, "generate", "Write a 2-sentence trade analysis: Cowboys send their 1st round pick to the Bills for a 90 OVR WR."),
    (Tier.SONNET, "generate", "Compare the passing stats of the top 3 QBs in the league this season."),
    (Tier.SONNET, "generate", "What strategies work best in Madden sim leagues for rebuilding teams?"),
    (Tier.SONNET, "generate", "Explain the TSL Elo rating system and how it affects sportsbook odds."),
    (Tier.SONNET, "generate", "Draft a rivalry recap between the Cowboys and Eagles spanning 5 seasons."),
    (Tier.SONNET, "generate", "What makes a good Madden sim league trade offer?"),
    (Tier.SONNET, "generate", "Analyze this trade: Team A gives 85 OVR CB + 3rd round pick. Team B gives 88 OVR WR. Who wins?"),
    (Tier.SONNET, "generate", "Write a power rankings blurb for a team that went 10-6 and lost in the divisional round."),
    (Tier.SONNET, "generate", "Explain how weekly game scheduling works in the MaddenStats API."),
    (Tier.SONNET, "generate", "What are the key differences between Madden sim and competitive play?"),
    (Tier.SONNET, "generate", "Generate a scouting report for a 78 OVR rookie QB with Superstar dev trait."),
    (Tier.SONNET, "generate", "Summarize what ATLAS does as a Discord bot for TSL."),
    (Tier.SONNET, "generate", "Write a one-paragraph season recap for a team that went 14-2 and won the Super Bowl."),
    (Tier.SONNET, "generate", "What is the commissioner's role in resolving disputes in TSL?"),

    # ── SONNET with system prompts (persona-driven) ──
    (Tier.SONNET, "system", "Give a hot take about the worst team in the league.", "You are ATLAS, the sharp-tongued commissioner AI for The Simulation League. Speak in third person as ATLAS. Be punchy — 2 sentences max."),
    (Tier.SONNET, "system", "Announce the trade deadline is tomorrow.", "You are ATLAS, the official voice of TSL. Formal commissioner tone. Brief and authoritative."),
    (Tier.SONNET, "system", "React to a 62-0 blowout game.", "You are ATLAS. Analytical tone. Cite the score and implications."),
    (Tier.SONNET, "system", "Welcome a new owner to TSL.", "You are ATLAS. Casual and encouraging. 2-3 sentences."),
    (Tier.SONNET, "system", "Comment on a controversial 4th down call.", "You are ATLAS. Rulebook mode — cite that 4th down rules exist, be definitive."),

    # ── OPUS tier (complex reasoning) ──
    (Tier.OPUS, "generate", "Design a scoring formula that balances wins, point differential, strength of schedule, and playoff performance for TSL power rankings. Explain the weights."),
    (Tier.OPUS, "generate", "Write a detailed 3-paragraph analysis of how a salary cap system could be implemented in a Madden sim league."),
    (Tier.OPUS, "generate", "Create a comprehensive scouting framework for evaluating Madden sim league draft prospects across all positions."),

    # ── generate_with_search (Gemini-primary, Google Search grounding) ──
    (Tier.SONNET, "search", "What are the latest Madden NFL roster updates?"),
    (Tier.SONNET, "search", "Find recent NFL trade news that could affect Madden sim leagues."),
    (Tier.SONNET, "search", "What Madden NFL 25 gameplay patches have been released recently?"),
    (Tier.SONNET, "search", "Search for tips on running a successful Madden sim league in 2026."),
    (Tier.SONNET, "search", "What are the current NFL power rankings?"),

    # ── Edge cases ──
    (Tier.HAIKU, "generate", ""),  # Empty prompt
    (Tier.HAIKU, "generate", "x"),  # Minimal prompt
    (Tier.SONNET, "generate", "A" * 500),  # Long prompt
    (Tier.HAIKU, "generate", "🏈 TSL 🏆 emoji test 🎮"),  # Unicode/emoji
    (Tier.SONNET, "generate", "Explain TSL rules.\n\nInclude:\n- Trade rules\n- 4th down\n- Blowout policy"),  # Multiline
    (Tier.HAIKU, "json", "Return JSON: {\"test\": true, \"nested\": {\"a\": 1}}"),  # Nested JSON
    (Tier.SONNET, "generate", "SELECT * FROM games WHERE season=95; -- Is this valid TSL SQL?"),  # SQL-like input
]

async def run_stress_test():
    results = {"pass": 0, "fail": 0, "fallback": 0, "errors": []}
    total = len(QUESTIONS)
    start_all = time.time()

    print(f"\n{'='*60}")
    print(f"  ATLAS AI Stress Test — {total} questions")
    print(f"{'='*60}\n")

    for i, q in enumerate(QUESTIONS, 1):
        tier = q[0]
        mode = q[1]
        prompt = q[2]
        system = q[3] if len(q) > 3 else None

        label = f"[{i:02d}/{total}] {tier.name:6s} {mode:8s}"
        short_prompt = (prompt[:50] + "...") if len(prompt) > 50 else prompt
        short_prompt = short_prompt.replace("\n", " ")

        try:
            start = time.time()

            if mode == "json":
                result = await atlas_ai.generate(
                    prompt=prompt, tier=tier, json_mode=True,
                    max_tokens=200, temperature=0.1
                )
            elif mode == "search":
                result = await atlas_ai.generate_with_search(
                    prompt=prompt, system=system or "",
                    max_tokens=300,
                )
            elif mode == "system":
                result = await atlas_ai.generate(
                    prompt=prompt, system=system or "", tier=tier,
                    max_tokens=200, temperature=0.7
                )
            else:
                result = await atlas_ai.generate(
                    prompt=prompt, tier=tier,
                    max_tokens=200, temperature=0.5
                )

            elapsed = time.time() - start
            provider = result.provider
            fb = " [FALLBACK]" if result.fallback_used else ""
            text_len = len(result.text) if result.text else 0

            if result.fallback_used:
                results["fallback"] += 1

            if text_len > 0:
                results["pass"] += 1
                status = "PASS"
            else:
                results["fail"] += 1
                status = "EMPTY"
                results["errors"].append(f"Q{i}: Empty response for '{short_prompt}'")

            print(f"  {label} {status:5s} {elapsed:5.1f}s {provider:12s}{fb} ({text_len} chars) | {short_prompt}")

        except Exception as e:
            elapsed = time.time() - start
            results["fail"] += 1
            err_msg = str(e)[:80]
            results["errors"].append(f"Q{i}: {err_msg}")
            print(f"  {label} FAIL  {elapsed:5.1f}s ERROR: {err_msg} | {short_prompt}")

    elapsed_all = time.time() - start_all

    print(f"\n{'='*60}")
    print(f"  Results: {results['pass']}/{total} passed, {results['fail']} failed")
    print(f"  Fallbacks: {results['fallback']}/{total} used Gemini fallback")
    print(f"  Total time: {elapsed_all:.1f}s ({elapsed_all/total:.1f}s avg)")
    print(f"{'='*60}")

    if results["errors"]:
        print(f"\n  Errors:")
        for e in results["errors"]:
            print(f"    - {e}")

    print()
    return results["fail"] == 0

if __name__ == "__main__":
    ok = asyncio.run(run_stress_test())
    sys.exit(0 if ok else 1)
