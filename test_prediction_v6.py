"""Test script to render prediction market V6 cards for preview."""

import asyncio
import sys
import os

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


async def main():
    from casino.renderer.prediction_html_renderer import (
        render_market_list_card,
        render_market_detail_card,
    )

    # ── Test data: 5 markets for list view ──
    markets = [
        {
            "title": "Will Bitcoin exceed $150,000 by July 2026?",
            "category": "🪙 Crypto",
            "yes_price": 0.42,
            "no_price": 0.58,
            "volume": 2_340_000,
            "end_date": "2026-07-01T00:00:00Z",
        },
        {
            "title": "Will the US Federal Reserve cut interest rates before September 2026?",
            "category": "💰 Economics",
            "yes_price": 0.71,
            "no_price": 0.29,
            "volume": 8_120_000,
            "end_date": "2026-09-01T00:00:00Z",
        },
        {
            "title": "Will GPT-5 be released by the end of 2026?",
            "category": "🤖 AI",
            "yes_price": 0.65,
            "no_price": 0.35,
            "volume": 5_600_000,
            "end_date": "2026-12-31T00:00:00Z",
        },
        {
            "title": "Will there be a US government shutdown before June 2026?",
            "category": "🏛️ Politics",
            "yes_price": 0.38,
            "no_price": 0.62,
            "volume": 1_200_000,
            "end_date": "2026-06-01T00:00:00Z",
        },
        {
            "title": "Will Apple announce a foldable iPhone in 2026?",
            "category": "📱 Tech",
            "yes_price": 0.15,
            "no_price": 0.85,
            "volume": 890_000,
            "end_date": "2026-12-31T00:00:00Z",
        },
    ]

    # ── Render list card ──
    print("Rendering market list card...")
    list_png = await render_market_list_card(markets, page=1, total_pages=4, filter_label="All Categories")
    with open("test_prediction_list.png", "wb") as f:
        f.write(list_png)
    print(f"  -> test_prediction_list.png ({len(list_png):,} bytes)")

    # ── Render detail card ──
    print("Rendering market detail card...")
    detail_png = await render_market_detail_card(
        title="Will Bitcoin exceed $150,000 by July 2026?",
        category="🪙 Crypto",
        yes_price=0.42,
        no_price=0.58,
        volume=2_340_000,
        liquidity=450_000,
        end_date="2026-07-01T00:00:00Z",
        user_position="YES",
        user_contracts=5,
        user_cost=210,
    )
    with open("test_prediction_detail.png", "wb") as f:
        f.write(detail_png)
    print(f"  -> test_prediction_detail.png ({len(detail_png):,} bytes)")

    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
