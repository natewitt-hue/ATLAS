# Spiral Adversarial Review Progress

Started: 2026-04-09
Reviewer: Codex (codex-cli 0.118.0) via `task --prompt-file` with adapted adversarial-review prompt template

## Ring 0 (1 file)
- [x] bot.py

## Ring 1 — Core orchestration (6)
- [x] echo_cog.py
- [x] setup_cog.py
- [x] permissions.py
- [x] constants.py
- [x] atlas_ai.py
- [x] echo_loader.py

## Ring 1 — Flow & Economy (8)
- [x] flow_sportsbook.py
- [x] flow_wallet.py
- [x] flow_store.py
- [x] flow_live_cog.py
- [x] economy_cog.py
- [x] sportsbook_core.py
- [x] wager_registry.py
- [x] real_sportsbook_cog.py

## Ring 1 — Oracle / AI / Memory (10)
- [x] oracle_cog.py
- [x] oracle_memory.py
- [x] polymarket_cog.py
- [x] codex_cog.py
- [x] codex_utils.py
- [x] intelligence.py
- [x] lore_rag.py
- [x] conversation_memory.py
- [x] reasoning.py
- [x] affinity.py

## Ring 1 — Genesis / Sentinel / Build (7)
- [x] genesis_cog.py
- [x] sentinel_cog.py
- [x] awards_cog.py
- [x] roster.py
- [x] build_tsl_db.py
- [x] build_member_db.py
- [x] atlas_html_engine.py

## Ring 1 — Admin / Casino / Home (4)
- [x] boss_cog.py
- [x] god_cog.py
- [x] atlas_home_cog.py
- [x] casino/casino.py

## Ring 2 — Core utilities (15)
- [x] analysis.py
- [x] atlas_colors.py
- [x] atlas_home_renderer.py
- [x] atlas_send.py
- [x] atlas_style_tokens.py
- [x] atlas_themes.py
- [x] db_migration_snapshots.py
- [x] espn_odds.py
- [x] format_utils.py
- [x] odds_utils.py
- [x] player_picker.py
- [x] sportsbook_cards.py
- [x] store_effects.py
- [x] team_branding.py
- [x] trade_engine.py

## Ring 2 — Casino subsystem (12)
- [ ] casino/casino_db.py
- [ ] casino/games/blackjack.py
- [ ] casino/games/coinflip.py
- [ ] casino/games/crash.py
- [ ] casino/games/slots.py
- [ ] casino/play_again.py
- [ ] casino/renderer/casino_html_renderer.py
- [ ] casino/renderer/highlight_renderer.py
- [ ] casino/renderer/ledger_renderer.py
- [ ] casino/renderer/prediction_html_renderer.py
- [ ] casino/renderer/pulse_renderer.py
- [ ] casino/renderer/session_recap_renderer.py

## Orphans (26)
- [ ] ability_engine.py
- [ ] backfill_embeddings.py
- [ ] card_renderer.py
- [ ] codex_intents.py
- [ ] conftest.py
- [ ] embed_helpers.py
- [ ] espn_asset_scraper.py
- [ ] flow_audit.py
- [ ] flow_events.py
- [ ] flow_cards.py
- [ ] google_docs_writer.py
- [ ] ledger_poster.py
- [ ] oracle_agent.py
- [ ] oracle_analysis.py
- [ ] oracle_query_builder.py
- [ ] oracle_renderer.py
- [ ] pagination_view.py
- [ ] stress_test_ai.py
- [ ] stress_test_codex.py
- [ ] stress_test_history.py
- [ ] stress_test_vet.py
- [ ] test_all_renders.py
- [ ] test_oracle_stress.py
- [ ] test_prediction_v6.py
- [ ] test_query_builder.py
- [ ] upload_emoji.py
