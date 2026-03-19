"""
Vet test — 10 TSL questions with full answers displayed for accuracy review.
Run: python stress_test_vet.py
"""
import asyncio
import sys
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(override=True)

import atlas_ai
from atlas_ai import Tier

# 10 TSL-specific questions that can be fact-checked against real league data
QUESTIONS = [
    (Tier.SONNET, None, "Who are the NFC East teams in the NFL, and what makes this division historically competitive in Madden sim leagues?"),
    (Tier.SONNET, None, "Explain what devTrait values 0, 1, 2, and 3 mean in Madden NFL, and how they affect player development in a sim league."),
    (Tier.SONNET, None, "What are the ability budget rules in Madden? How many abilities can a Star, Superstar, and X-Factor player equip?"),
    (Tier.HAIKU, None, "In a Madden sim league, what is a '4th down complaint' and why do leagues regulate 4th down behavior?"),
    (Tier.SONNET, None, "What is an Elo rating system and how could it be used for sportsbook odds in a Madden sim league?"),
    (Tier.HAIKU, None, "What does 'status 2' vs 'status 3' mean for a game in the MaddenStats API?"),
    (Tier.SONNET, None, "Explain the role of a commissioner bot in a Madden sim league. What tasks does it automate?"),
    (Tier.SONNET, None, "What is the difference between a Normal, Star, Superstar, and X-Factor development trait in Madden NFL 25?"),
    (Tier.HAIKU, None, "In Madden, what is weekIndex and is it 0-based or 1-based?"),
    (Tier.SONNET, None, "Describe how a trade evaluation system might work in a Madden sim league — what factors should it consider?"),
]

async def main():
    print(f"\n{'='*70}")
    print(f"  ATLAS AI Accuracy Vet — 10 TSL Questions")
    print(f"{'='*70}\n")

    for i, (tier, system, question) in enumerate(QUESTIONS, 1):
        result = await atlas_ai.generate(
            prompt=question,
            tier=tier,
            system=system or "",
            max_tokens=400,
            temperature=0.3,
        )
        fb = " [FALLBACK]" if result.fallback_used else ""
        print(f"┌─ Q{i:02d} ({tier.name} via {result.provider}{fb})")
        print(f"│ {question}")
        print(f"├─ Answer:")
        # Word-wrap the answer for readability
        words = result.text.split()
        line = "│   "
        for w in words:
            if len(line) + len(w) + 1 > 78:
                print(line)
                line = "│   " + w
            else:
                line += " " + w if line.strip() != "" and line != "│   " else w if line == "│   " else line + " " + w
        if line.strip():
            print(line)
        print(f"└─ ({len(result.text)} chars)\n")

if __name__ == "__main__":
    asyncio.run(main())
