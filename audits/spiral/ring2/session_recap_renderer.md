# Adversarial Review: casino/renderer/session_recap_renderer.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 566
**Reviewer:** Claude (delegated subagent)
**Total findings:** 13 (1 critical, 5 warnings, 7 observations)

## Summary

Stateless HTML template renderer with good `esc()` discipline for user-controlled fields. The main risk is not in the HTML itself: the function accepts a live `PlayerSession` dataclass by reference and reads ~14 mutable attributes without a snapshot, while `flow_live_cog.SessionTracker` can mutate the same session from `_on_game_result` (executed inside an `asyncio.to_thread` on an event-loop worker). If a new `GameResultEvent` arrives while `render_session_recap` is formatting HTML, the card can show a numerically inconsistent snapshot (e.g., `total_games=6` but `wins+losses+pushes=5`). Secondary concerns: unescaped width injection into inline CSS, an unescaped `streak_html`/`best_streak_html`, missing divider fallback when both game breakdown and highlights are empty, and a loss-display sign bug on the edge.

## Findings

### CRITICAL #1: TOCTOU on live PlayerSession during render — stats can disagree with each other mid-render

**Location:** `casino/renderer/session_recap_renderer.py:140-543` (especially 149-215, 243-251, 503-515)
**Confidence:** 0.80
**Risk:** `_build_session_recap_html()` receives the *live* `PlayerSession` dataclass object (not a snapshot) and reads ~14 mutable fields in sequence (`net_profit`, `current_streak`, `best_streak`, `games_by_type`, `events`, `total_games`, `wins`, `losses`, `pushes`, `biggest_win`, `biggest_loss`, etc.). It is called from `flow_live_cog._post_session_recap()` after `collect_expired()` has removed the session from `SessionTracker._active` — but the CALLED session object is the same dataclass instance, and **nothing** prevents a late-arriving `GameResultEvent` handler (which calls `SessionTracker.record()` via `asyncio.to_thread`) from mutating that same `PlayerSession` while the renderer is still composing HTML on the event loop. The `SessionTracker` keys on `(discord_id, guild_id)`; if a second `GameResultEvent` with the same key arrives after `collect_expired()` removed the session but before the recap finishes rendering, a *new* `PlayerSession` is created in `_active`, so the renderer's object is no longer at risk from that specific mutation path. However, the renderer itself reads the session fields *interleaved with* `await render_card(html)` (line 566 — actually `_build_session_recap_html` is sync and the html is built before the await, so the interleaving is between `_build_session_recap_html`'s reads and any other concurrent mutation). The primary race window is: `_on_game_result` arrives for the same (uid, gid) while `_post_session_recap` has already captured the session reference but before `collect_expired()` runs — `SessionTracker.record()` goes through `_active.get(key)`, finds the still-tracked session, and mutates it. The Ring 1 `flow_live_cog` audit already flagged a SessionTracker race; this renderer does not defend against it.
**Vulnerability:** The renderer walks `session.wins` (L507), `session.losses` (L511), `session.pushes` (L515), `session.total_games` (L503/243), `session.net_profit` (L149/157/158), `session.events` (L214 via `_build_highlight_events`), `session.games_by_type` (L187), `session.biggest_win`/`biggest_loss` (L248-251), `session.current_streak` (L165-178), `session.best_streak` (L182) — all independent attribute reads. If a concurrent `record()` mutates `total_games` between the read at L243 and the read at L503, the win-rate denominator will diverge from the displayed game count. More subtly: `_build_highlight_events` iterates `session.events` twice (L86 and L103), and `SessionTracker.record()` reassigns the list (`self.events = self.events[-EVENTS_CAP:]` at flow_live_cog.py L101), so `getattr(ev, ...)` on a list that was just replaced can yield a mix of pre- and post-truncation elements.
**Impact:** User-visible inconsistency on session recap cards during peak activity: "Games: 6" but "Wins 3 / Losses 2 / Pushes 0" (sums to 5), wrong `win_rate%`, or a highlight event that references data from a game not counted in the totals. Rare but hard to reproduce and user-facing.
**Fix:** Snapshot the session before handing it to the renderer. Either (a) change the signature to accept a plain dict produced by `session.to_dict()` under a lock, or (b) at the top of `_build_session_recap_html`, copy every field into local variables immediately (`total_games = session.total_games`, `events = list(session.events)`, etc.) and use only the locals below. Option (a) is safer because it forces callers to think about the snapshot boundary. Either way, wrap the snapshot in an `asyncio.Lock` or `threading.Lock` on `SessionTracker` so `record()` and snapshot are mutually exclusive.

### WARNING #1: `win_rate` injected into inline CSS `width:` without bounds/escaping — CSS injection vector

**Location:** `casino/renderer/session_recap_renderer.py:241-244, 522-524`
**Confidence:** 0.55
**Risk:** `win_rate` is computed from `round(session.wins / session.total_games * 100)`. It then flows into `<div class="win-rate-bar-fill" style="width:{win_rate}%"></div>` (L523). `session.wins` and `session.total_games` are mutated by `SessionTracker.record()` from ingestor cogs — they should always be non-negative ints, but the dataclass has no type enforcement; a buggy game cog that emits a negative `wins` (or a corrupted row from `flow_live_sessions.events` JSON — `from_dict` does no validation, flow_live_cog.py L149-155) would produce a negative `win_rate`. Rendered CSS becomes `width:-4%` which Playwright interprets as invalid, clamping to 0 in most browsers, but `win_rate > 100` from a similarly-corrupted row yields `width:150%` which is silently clamped but may overflow the container. More serious: if `from_dict` ever accepted a string-typed value (JSON parse from `events` blob could theoretically produce this), `{win_rate}` format could become `width:</style><script>...%`. The current code guards this only by relying on `round()` returning an int, which depends on `session.wins` and `session.total_games` being numbers — not enforced anywhere.
**Vulnerability:** L243-244 and L525 do not call `esc()` on `win_rate`. No clamp `max(0, min(100, win_rate))`. The `session.wins` and `session.total_games` inputs come from deserialized DB state (`flow_live_cog.PlayerSession.from_dict` at flow_live_cog.py L141-160), which does `d.get("wins", 0)` with zero type coercion. If a mis-typed event row ever lands in the DB (e.g., via a future migration or manual poke), it flows here unsanitized.
**Impact:** Low as of today (inputs are int-typed by construction). Medium if the JSON persistence path ever round-trips a non-int or a future `from_dict` caller injects a string. CSS/HTML injection surface even at low probability is worth closing.
**Fix:** `win_rate = max(0, min(100, int(win_rate)))` before use; use `int(win_rate)` in the f-string; or defensively `esc(str(int(win_rate)))`. Also add type coercion in `_build_session_recap_html`'s first few lines for every int field.

### WARNING #2: `streak_html`, `best_streak_html`, `game_breakdown_html`, `highlights_html`, `commentary_html`, `best_cells_html` inserted raw into template — trusts upstream builders

**Location:** `casino/renderer/session_recap_renderer.py:475-541`
**Confidence:** 0.6
**Risk:** The body template at L262-541 interpolates six pre-built HTML fragments without escaping (`{streak_html}`, `{best_streak_html}`, `{game_breakdown_html}`, `{highlights_html}`, `{commentary_html}`, `{f'<div class="data-grid cols2">{best_cells_html}</div>' ...}`). Each builder (L164-183, L186-211, L213-232, L235-239, L247-260) is expected to have already called `esc()` on user-controlled values. Auditing them:
  - `streak_html` (L175, 178): does NOT escape `icon`/`label`/`s` — but they are computed from `session.current_streak` (int) and hardcoded HTML entities, so safe today unless `current_streak` is ever a string.
  - `best_streak_html` (L183): `session.best_streak` embedded unescaped — int, safe today.
  - `game_breakdown_html` (L194-199, L202-205): `label` is `esc()`'d (L196), `count` is raw — `count` comes from `games_by_type` defaultdict values (ints from `session.record`), safe today. `icon` comes from `GAME_ICONS` (hardcoded) or literal fallback `&#127922;` (raw HTML entity from `GAME_LABELS.get(game_type, game_type.title())` path — but `game_type.title()` at L192 is embedded via `esc(label)` so fine). Wait — L191: `label = GAME_LABELS.get(game_type, game_type.title())` — if `game_type` is an attacker-controlled string, `game_type.title()` is unsanitized before `esc` on L196. `game_type` originates from `GameResultEvent.game_type` which is code-set from cogs, so technically trusted, but fragile.
  - `highlights_html` (L219-227): `h["note"]`, `h["label"]`, `h["amount"]`, `h["color"]` are all `esc()`'d. `h["icon"]` is NOT — it comes from `GAME_ICONS.get(game_type, "&#127922;")` (hardcoded HTML entity or lookup-only key), safe today.
  - `commentary_html` (L239): `esc(commentary)` — fine.
  - `best_cells_html` (L249-260): `session.biggest_win` and `session.biggest_loss` embedded as raw ints — safe today since they are ints from `record()`.
**Vulnerability:** Every safety claim above depends on the upstream type invariants. Any future cog that emits a `GameResultEvent` with a stringly-typed field (or a manual DB poke into `flow_live_sessions.events`) flows through `_dict_to_event` (flow_live_cog.py L61-74) which does `d.get("game_type", "unknown")` and `d.get("multiplier", 1.0)` with zero coercion. If `game_type` ever becomes `"<script>x</script>"`, the `GAME_LABELS.get(game_type, game_type.title())` fallback + L111 (`esc(label)`) catches the label but not `game_type.title()` when embedded elsewhere — actually the `label` IS esc'd, so this specific path is safe.
**Impact:** Zero today, fragile under any upstream type regression. This is a contract that is not enforced.
**Fix:** Add type coercion at the top of `_build_session_recap_html`: `total_games = int(getattr(session, 'total_games', 0))` etc., and add `esc()` calls inside the HTML builders themselves for defence in depth, even on fields believed to be ints. Or better: create a `_SessionSnapshot` pydantic/dataclass with strict types and require callers to pass that.

### WARNING #3: `<div class="gold-divider"></div>` at L531 always emitted even when both following sections are empty — trailing divider artifact

**Location:** `casino/renderer/session_recap_renderer.py:529-541`
**Confidence:** 0.85
**Risk:** L531 unconditionally emits a `<div class="gold-divider"></div>`. The sections after it (game_breakdown_html L534, highlights_html L537, commentary_html L540) are each conditionally populated. If `session.games_by_type` is empty AND `_build_highlight_events` returns `[]` AND `commentary == ""`, the divider is emitted with nothing following — producing a visually broken trailing separator bar. `_post_session_recap` at flow_live_cog.py L681 gates on `session.total_games < 2`, so this can reach the renderer with 2+ games. Games are always added to `games_by_type` on `record()` (flow_live_cog.py L98), so `games_by_type` is non-empty in the normal flow — but a session loaded from `from_dict` with missing `games_by_type` key (L158: `defaultdict(int, d.get("games_by_type", {}))`) could be empty if the persisted JSON was corrupted. Lower-probability case: all events are pushes (`highlights` still has entries because `_build_highlight_events` scores all events, not just winners) — so `highlights` should still be non-empty unless `session.events` list was truncated to empty (also unlikely).
**Vulnerability:** No guard for the "all three conditional blocks empty" case.
**Impact:** Visual artifact on unusual sessions. Cosmetic, low impact.
**Fix:** Move the divider inside a conditional: `{"<div class='gold-divider'></div>" if (game_breakdown_html or highlights_html or commentary_html) else ""}`.

### WARNING #4: `biggest_loss` sign-handling ambiguity — `0` vs negative falls into the same branch

**Location:** `casino/renderer/session_recap_renderer.py:247-260`
**Confidence:** 0.7
**Risk:** L248 checks `if session.biggest_win or session.biggest_loss:` — both are integers. In `SessionTracker.record()` (flow_live_cog.py L109-117), `biggest_loss` is only updated via `if profit < self.biggest_loss` where the initial value is `0`, so `biggest_loss` ends up as **0 or a negative integer**. That's consistent. But L251 does `f"-${abs(session.biggest_loss):,}"` when `biggest_loss` is truthy (i.e., non-zero). The comment at L250 says "`biggest_loss` is stored as a negative integer; abs() converts to display magnitude" — correct when it IS negative. But what if a buggy persistence layer or a future code path sets `biggest_loss` to a positive integer (e.g., someone "fixes" the semantics to magnitude)? Then L251 displays `-$X` for what is actually a positive "biggest loss magnitude", which may or may not match the rest of the card. More immediately: `sportsbook_cards._get_season_start_balance()` has a known column-may-not-exist hazard (per CLAUDE.md) — similar fragility applies here to any schema migration where `biggest_loss` semantics change.
**Vulnerability:** Tightly coupled to the sign convention in `flow_live_cog.PlayerSession.record()`. No assertion. No comment in the schema.
**Impact:** Edge case, but the card is user-visible. Wrong sign on the "worst loss" cell would embarrass ATLAS and undermine trust in the card numbers.
**Fix:** `bl_str = f"-${abs(session.biggest_loss):,}" if session.biggest_loss < 0 else "—"`. Explicit negativity check, not `biggest_loss` truthiness.

### WARNING #5: `_build_highlight_events` sorts by `abs(net)` + boost, ties broken by Python's stable sort on (score, event) — but `scored.sort` compares the tuples, which falls through to comparing `GameResultEvent` instances on tie

**Location:** `casino/renderer/session_recap_renderer.py:85-99`
**Confidence:** 0.85
**Risk:** L85: `scored: list[tuple[int, object]] = []`. L99: `scored.sort(key=lambda x: x[0], reverse=True)`. The `key=lambda x: x[0]` actually fixes the tuple comparison issue — so ties are resolved by Python's stable sort (input order preserved). OK so far. But there's a **different** bug: the score for losses is `abs(net)` where `net = event.net_profit = payout - wager`. For a loss, `payout=0` (typically), `wager=500`, so `net = -500`, `abs(net) = 500`. For a win, `payout=1000`, `wager=500`, `net = 500`, `abs(net) = 500`. A loss and a win with the same magnitude score equally. If you have 3 losses at $500 each and 1 win at $600, the highlights will be: win $600 → loss $500 → loss $500. User asked for "top 3 notable events", but the UI implies "best moments". Shoving two losses into the highlights row on a losing session is probably fine (sad, but accurate). More subtly: `extra.get("blackjack")` boost +10k — this fires on any blackjack, including a blackjack **loss** (if the dealer also had one? or on a push?). Is a losing blackjack "notable"? Arguably yes. But a push-blackjack would rank very high (10k boost) yet show "$0" amount — confusing UX.
**Vulnerability:** Mixing magnitude with boost collapses the distinction between "big win" and "big loss". No tests verify the ordering.
**Impact:** UX confusion. User sees highlights that don't match their intuition ("why is my worst loss in the highlights reel?"). Low severity.
**Fix:** Separate "good" events from "bad" events and pick top-N from each, or score with a signed formula that prioritizes wins (`score = net + boost` rather than `abs(net) + boost`). At minimum, fix the push-blackjack boost to not fire when `net == 0`.

### OBSERVATION #1: Zero-`total_games` path still reachable via `_post_session_recap` guard bypass

**Location:** `casino/renderer/session_recap_renderer.py:241-244`
**Confidence:** 0.9
**Risk:** L242-244 guards `if session.total_games > 0` before computing `win_rate = round(session.wins / session.total_games * 100)`. This is correct defensively, but `flow_live_cog._post_session_recap` (L681) already gates on `session.total_games < 2` and returns early. So this renderer should never be called with `total_games == 0` in production, but the guard is retained. That's fine — defence in depth. **Observation**: there's no assertion or log when `total_games == 0` reaches the renderer, which would indicate a bug in the caller.
**Vulnerability:** None directly. Dead safety code, possibly intentional.
**Impact:** None. Mentioned for completeness.
**Fix:** Consider adding `log.warning("render_session_recap called with total_games=0")` when this branch is hit, so upstream bugs surface.

### OBSERVATION #2: `session_reaper` runs every 30s — stale session data leaks into multiple recap cards on bot restart

**Location:** `casino/renderer/session_recap_renderer.py:548-566` (indirect via caller)
**Confidence:** 0.7
**Risk:** `flow_live_cog.session_reaper` (L507-513) fires every 30 s and hands expired sessions to `_post_session_recap` → `render_session_recap`. But `SessionTracker.load_persisted()` (flow_live_cog.py L244-267) restores from `flow_live_sessions` on cog load, and the restored sessions' `last_activity` is the timestamp from before the restart. After restart, every session that was dormant >300s will be reaped within 30s and dumped as recaps. If the bot was down for a day, dozens of stale sessions could trigger a recap storm — 20+ concurrent `render_card()` calls against a 4-page Playwright pool, starving other renders and spamming the `#flow-live` channel.
**Vulnerability:** This renderer doesn't cause the problem directly, but it's the sink that amplifies it. The card cost is real (700px×~1000px at 2x DPI = heavy screenshot).
**Impact:** Post-restart recap spam, Playwright pool starvation, possible rate-limit hit on Discord channel.
**Fix:** Not in this file. `load_persisted()` should drop sessions whose `last_activity` is older than some cutoff (e.g., 1 hour), or batch-render recaps with throttling. Note here as cross-cutting.

### OBSERVATION #3: `duration` label shows "< 1m" for negative or zero durations but doesn't log the anomaly

**Location:** `casino/renderer/session_recap_renderer.py:51-59`
**Confidence:** 0.8
**Risk:** `_format_duration` uses `max(0, int(last_activity - started_at))` — so a clock-skew case where `last_activity < started_at` silently coerces to 0. This could mask a real bug where timestamps got swapped (e.g., a persistence bug in `_persist` that writes `started_at` after `last_activity`). No log, no warning.
**Vulnerability:** Silent coercion of potentially-buggy input.
**Impact:** Low. The card just shows "< 1m" and hides the anomaly.
**Fix:** `if last_activity < started_at: log.warning("session_recap: inverted timestamps uid=? ...")`. Pass `uid` through if needed.

### OBSERVATION #4: Magic numbers for scoring thresholds hardcoded in `_build_highlight_events`

**Location:** `casino/renderer/session_recap_renderer.py:92-96`
**Confidence:** 0.9
**Risk:** `100_000` (jackpot boost), `10_000` (blackjack boost), `5_000` (5x+ multiplier boost), `5.0` (multiplier cutoff) are all literals with no named constants. Not CRITICAL, but makes tuning opaque. No documentation on why jackpot boost is 10× blackjack boost.
**Vulnerability:** Tuning friction, not a bug.
**Impact:** None runtime.
**Fix:** Extract module-level constants: `JACKPOT_SCORE_BOOST = 100_000`, etc.

### OBSERVATION #5: `multiplier >= 10.0` and `multiplier >= 2.0` branches produce the same label format — dead tier

**Location:** `casino/renderer/session_recap_renderer.py:119-122`
**Confidence:** 0.95
**Risk:** L119-122 has `elif multiplier >= 10.0: note = f"{multiplier:.1f}x"` and `elif multiplier >= 2.0: note = f"{multiplier:.1f}x"`. Both branches do the exact same formatting. The 10.0 branch is dead code — it's never distinguishable from the 2.0 branch. Presumably the intent was a different label or emphasis for 10x+ (e.g., "MEGA 12.5x"), but the divergent formatting was never implemented.
**Vulnerability:** Dead code, author likely forgot to finish.
**Impact:** None runtime. Design intent lost.
**Fix:** Either collapse into a single branch `elif multiplier >= 2.0: note = f"{multiplier:.1f}x"` OR differentiate the `>= 10.0` label (e.g., `note = f"MEGA {multiplier:.1f}x"`).

### OBSERVATION #6: `highlights` list starts empty, but no feedback if events exist yet all score 0 and are filtered out

**Location:** `casino/renderer/session_recap_renderer.py:74-135`
**Confidence:** 0.7
**Risk:** `_build_highlight_events` returns up to 3 scored events, but if every event has `net_profit = 0` (all pushes) and no jackpot/blackjack/multiplier boost, all scores are 0 — the sort still returns 3 events, but they are all ties. Stable sort preserves order. Fine for correctness. But the code path at L95 (`multiplier >= 5.0` boost) checks `getattr(ev, 'multiplier', 1.0)` — for a push event with `multiplier = 1.0`, no boost. Then at L119 (`multiplier >= 10.0`) the push event fails all multiplier branches and at L123 `outcome == "push"` sets `note = "PUSH"`. That's fine. But what if `outcome` is a weird string like `"cashout"` (crash game) that's not "win"/"loss"/"push"? Then L126 color = `"green" if net > 0 else "red" if net < 0 else "amber"` still works based on `net`. No explicit handling for unknown outcome strings.
**Vulnerability:** No assertion or default on unknown `outcome` values.
**Impact:** None today. Fragile if new game types introduce new outcome strings.
**Fix:** Add a whitelist check on `outcome` and log unknowns. Minor.

### OBSERVATION #7: No `display_name` length cap — long Discord names can overflow the card header

**Location:** `casino/renderer/session_recap_renderer.py:479`
**Confidence:** 0.8
**Risk:** `display_name` is passed directly to `esc(display_name)` on L479. Discord display names can be up to 32 characters. The `.player-name` CSS at L276 uses `font-size: 20px` with no `max-width` or `text-overflow: ellipsis`. A 32-char name combined with a streak badge on the right can overflow the `sr-header` flex container (`justify-content: space-between`). The header may wrap or push content off the 700px card.
**Vulnerability:** No truncation, no ellipsis, no max-width on `.player-name`.
**Impact:** Visual overflow on cards for users with long names + high streaks.
**Fix:** Truncate `display_name` to 24 chars with ellipsis before escaping, or add `max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;` to `.player-name` CSS.

## Cross-cutting Notes

- **Ring 1 echo**: Ring 1 flagged the `SessionTracker` race. This renderer does NOT defend against it — it reads mutable session state live. Any fix to `SessionTracker` concurrency must provide a snapshot API that the renderer uses, OR this renderer must make its own copy at entry. Recommend touching both files in the same PR.
- **Pattern across casino renderers**: The `streak_html`, `best_streak_html`, `game_breakdown_html`, `highlights_html`, `commentary_html` pattern (pre-building HTML fragments and splicing them into a template) is shared with `highlight_renderer.py`, `pulse_renderer.py`, and `casino_html_renderer.py`. The same trust-upstream-builder-is-safe assumption applies to all of them. A single `_session_snapshot` + type-coercion helper would de-risk the whole batch.
- **`<div class="gold-divider"></div>` trailing-artifact bug at L531** likely repeats in sibling renderers that use the same divider pattern. Worth grep'ing for `gold-divider` across `casino/renderer/*.py`.
- **Clock-skew silent swallow in `_format_duration`** is a common ATLAS pattern (multiple files use `max(0, ...)` on time deltas with no log). Consider adding a shared `_safe_duration_seconds()` helper that logs anomalies once.
