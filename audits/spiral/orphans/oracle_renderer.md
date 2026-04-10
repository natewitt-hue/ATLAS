# Adversarial Review: oracle_renderer.py

**Verdict:** needs-attention
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 482
**Reviewer:** Claude (delegated subagent)
**Total findings:** 11 (2 critical, 5 warnings, 4 observations)

**ORPHAN STATUS: LIVE**
This file is not imported through bot.py's direct dependency chain but IS imported by active code: `oracle_cog.py`. Argus's static scan missed it because the bot.py spiral doesn't trace through cogs' indirect imports. Review as active production code.

## Summary

Oracle card renderer with two notable HTML-escape bugs: one re-unescapes `&amp;` after `html.escape()` converts it (defeating escaping), and another passes unescaped numeric values and style strings into an inline CSS context that has no defense against CSS/HTML injection. The renderer also crashes on empty `result.title` / missing metadata when `result` attribute access assumes shape, and depends on `wrap_card` + `render_card` behavior without any timeout. Not a security hole at rest because inputs come from internal AnalysisResult objects, but any future code path that routes user text into `result.title`, `result.prediction`, or `comparison_data["name"]` without pre-sanitization opens injection paths.

## Findings

### CRITICAL #1: `_build_analysis_body` undoes HTML escaping by manually replacing `&amp;` → `&` before applying `_discord_md_to_html`
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:350-352`
**Confidence:** 0.95
**Risk:** The line is: `content_html = _discord_md_to_html(html_mod.escape(raw_content).replace("&amp;", "&"))`. This:
1. HTML-escapes the raw content (`<` → `&lt;`, `>` → `&gt;`, `&` → `&amp;`, `"` → `&quot;`, `'` → `&#x27;`).
2. Then immediately replaces `&amp;` with `&` — undoing one-fifth of the escaping.
3. Then applies markdown conversion.

The result: `&` in user content is NOT escaped, and any HTML entity in the raw content is preserved verbatim. If `raw_content` contains `&lt;script&gt;alert(1)&lt;/script&gt;` (already-escaped from an upstream source), it survives this pipeline and renders as a literal `<script>` tag in the final PNG — because after `html.escape` it becomes `&amp;lt;script&amp;gt;...` then the `.replace` strips the `&amp;` → `&lt;script&gt;...` which the browser treats as the `<` character (in the HTML rendering step).

Worse: `_discord_md_to_html` uses regex substitutions that produce raw HTML tags (`<b>`, `<i>`, `<div class="analysis-h2">`). If the upstream `raw_content` contains markdown that opens a tag but never closes it (`**unfinished`), the pattern fails silently and the content is rendered as-is — but crucially, the `.replace("&amp;", "&")` has already undone escaping on all ampersands, so any literal HTML from a pre-escaped source is now unescaped.
**Vulnerability:** The `raw_content` comes from `section.get("content", "")` where `section` is a field of `result.sections`. `result` is an `AnalysisResult` returned by the Oracle AI pipeline. If any Oracle analysis ever includes user-controlled text (team names, player names, owner names — all of which come from API data that includes underscores and spaces but could, in theory, include HTML-hostile characters), that text flows through this path and gets partially unescaped. Even without user input, a team name containing `&` in the API now produces malformed output.
**Impact:** Broken rendering at best, HTML injection into a PNG at worst. Because the HTML is then rendered by Playwright into a PNG, an attacker cannot directly exfiltrate data via JS, but can still inject arbitrary visual content into the card (fake predictions, fake records, arbitrary styling).
**Fix:** Remove the `.replace("&amp;", "&")`. If the intent was to allow already-escaped HTML entities to pass through, use a proper sanitizer like `bleach.clean` with an allowlist of tags.

### CRITICAL #2: `comparison_data["name"]` and raw OVR/record values are interpolated directly into inline CSS style strings with no escape
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:305-316, 378-403`
**Confidence:** 0.80
**Risk:** The f-strings at lines 305-316 and 378-403 build inline `style="..."` attributes with values like:
- `width:{pct_a}%;background:{accent};`
- `color:{accent}1A;`
- `background:{accent}18;`

where `accent` is a Token color and `pct_a` is a computed percent. If `accent` is ever user-controlled (it is not today — it comes from `_TYPE_META`) or if the computation produces a NaN or inf, the style string becomes invalid CSS. The more interesting bug is at line 389-390: `n_a = esc(str(ta_m.get("name", "")))` and `n_b = esc(str(tb_m.get("name", "")))` — these use `esc()` (HTML escape) before interpolation into an HTML text context, which is safe. But `ovr_a`, `ovr_b`, `wp_a`, `wp_b`, and the computed `prob_a` / `pct_a` are NOT escaped before being interpolated into the style string at lines 394-402.
**Vulnerability:** The math at lines 384-389 wraps NaN/Inf guards: `float(str(ta_m.get("ovr", 85)).replace("?", "85"))`. If `ta_m["ovr"]` is a string like `"85; background:url(javascript:...);` — a hypothetical hostile API payload — the `float()` call raises ValueError, caught at line 404's bare `except`, and the entire winprob block silently vanishes. The CSS context itself is technically safe because `pct_a` and `pct_b` are ints from `round()`. But the pattern of interpolating raw `"accent"` string values into CSS is fragile and will silently fail in ways hard to diagnose. Additionally, `ta_m.get("ovr", 85)` falls back to 85 silently rather than logging — so missing metadata produces quietly-wrong rendering rather than a visible error.
**Impact:** Silent rendering of incorrect win probabilities when OVR is missing. Silent drops of winprob block on any unexpected input. If a future change lets users set `"accent"` colors per theme, direct CSS injection is possible.
**Fix:** Guard the math with explicit None checks before `float()`. Log when fallbacks fire. Validate `accent` is a 7-character hex string before interpolation. Use `style` object construction rather than f-string concatenation for user-adjacent values.

### WARNING #1: `result.sections`, `result.prediction`, `result.metadata`, `result.title`, `result.comparison_data` accessed with no guarding on shape
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:333-439`
**Confidence:** 0.90
**Risk:** The renderer assumes `result` is a fully-populated dataclass with all fields. No `getattr` fallbacks for `result.title` (line 335), `result.sections` (line 348), `result.metadata` (line 339, 459-460), `result.prediction` (line 361), `result.confidence` (line 363). If any of these are None or missing, `AttributeError` crashes the renderer.
**Vulnerability:** `getattr(result, "comparison_data", None)` is used at lines 343 and 374, showing the author knew some fields are optional — but the other fields are NOT guarded the same way. `result.metadata` could be None, in which case `result.metadata.get(...)` raises AttributeError.
**Impact:** Any partial AnalysisResult (from an aborted analysis run or a new type that doesn't populate all fields) crashes the card pipeline. The crash bubbles up to `render_card()` which may leak a Playwright page back to the pool in a broken state.
**Fix:** Defensive `getattr(result, "sections", []) or []`, `getattr(result, "metadata", {}) or {}`, etc.

### WARNING #2: `_discord_md_to_html` uses regex with non-greedy patterns that can match across newlines unexpectedly
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:319-330`
**Confidence:** 0.70
**Risk:** The italic regex `r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)'` has no `re.DOTALL` flag, so `.` doesn't match newlines — good. But the bold regex `r'\*\*(.+?)\*\*'` at line 327 also has no flag and greedy behavior across a multiline input. If a section contains `**term1** and some text **term2**` on one line, fine. If it contains `**term1** \n some middle \n **term2**` across three lines, the bold regex captures across the newline because the input is not line-split — the whole section is one string.
**Vulnerability:** A section with two separate `**bold**` phrases on different lines may produce unexpected nesting. Rare, but the `_discord_md_to_html` pipeline has zero unit tests visible in the directory, so regressions are invisible.
**Impact:** Mis-rendered bold/italic in sections with multi-line content.
**Fix:** Split input by `\n` before applying the regex, or add explicit multiline tests.

### WARNING #3: `_cmp` returns `(False, False)` on `except Exception` — silently merges "equal" and "parse error"
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:225-236`
**Confidence:** 0.75
**Risk:** The nested `_cmp` helper in `_build_comparison_table_html` has an `except Exception: return False, False` that collapses all errors into "no edge." If `ta["rank"]` is a string like `"8.5"`, `float(...)` works; if it's `"Unranked"`, the try-block's `.replace("#", "").replace("+", "").strip()` doesn't clean it enough, `float("Unranked")` fails, and both columns show "no edge" — visually identical to "actually tied."
**Vulnerability:** The edge-bar at lines 262-265 computes `edges_a = sum(1 for *_, aw, bw in rows if aw)` — so a row where `_cmp` returned `(False, False)` contributes zero edges to either side. Parse errors silently skew the total-edges denominator and the displayed edge bar.
**Impact:** Comparison cards misrepresent which team has the statistical edge when one team has a missing value.
**Fix:** Log parse failures with the raw values. Use an `Unknown` marker in the table instead of silently falling back to "equal."

### WARNING #4: `render_card` and `wrap_card` have no timeout — a hung Playwright page blocks the request
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:468-470`
**Confidence:** 0.60
**Risk:** `return await render_card(full_html)` has no timeout. If the Playwright page pool is exhausted or a page hangs during rendering (runaway CSS, infinite animation in a theme splice), this await can block forever.
**Vulnerability:** The renderer doesn't know how `render_card` handles hung pages. Per CLAUDE.md, the pool has 4 pre-warmed pages; if one hangs, the pool shrinks. Over time, the pool runs out and every render stalls.
**Impact:** Oracle commands silently hang. User sees a loading indicator until Discord times the interaction out at 15 min.
**Fix:** `await asyncio.wait_for(render_card(full_html), timeout=30.0)` and catch `TimeoutError` to return a fallback embed.

### WARNING #5: `_sparkline_svg` is defined but never called anywhere in this file
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:196-217`
**Confidence:** 0.95
**Risk:** A 21-line helper that constructs an SVG sparkline, and grep-search inside this file shows zero callers of `_sparkline_svg`. It's dead code.
**Vulnerability:** Dead code drift is a smell. If the author intended to use sparklines in a future card type but never wired them up, the code bitrots and may crash when a new caller finally uses it.
**Impact:** Code bloat, maintenance tax.
**Fix:** Either wire it into a card type or delete it.

### OBSERVATION #1: `import re` inside `_discord_md_to_html` is redundant — `re` could be imported at module level
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:319-321`
**Confidence:** 0.90
**Risk:** Importing inside the function is executed on every call. For a hot-path renderer, that's a minor perf hit but more importantly a style smell.
**Fix:** Move `import re` to the top of the file. There's no circular-import risk with the standard library.

### OBSERVATION #2: `ATLAS_ICON_URL` is imported but never used
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:25-28`
**Confidence:** 0.95
**Risk:** Try-import for a constant that's not referenced anywhere in the file.
**Fix:** Delete the import or actually use the icon in the footer.

### OBSERVATION #3: `render_oracle_card_to_file` opens BytesIO, seeks to 0, and returns — but never closes the buffer
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:473-482`
**Confidence:** 0.60
**Risk:** `discord.File(buf, ...)` takes ownership of the buffer, so explicit close is handled by discord.py. But `io.BytesIO` holds the bytes in memory until GC. Not a leak per se, but a design observation.
**Fix:** None required if discord.py handles it. Document the ownership transfer.

### OBSERVATION #4: `_build_analysis_body` has a single-line return with a 17-line f-string — hard to read and diff
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_renderer.py:422-439`
**Confidence:** 0.40
**Risk:** Style / maintainability. A future change will cause a large diff.
**Fix:** Use template literals or string join for multi-line HTML templates.

## Cross-cutting Notes

The HTML-escape double-unescape bug at line 352 is the same class of issue that the Ring 1 `atlas_html_engine` audit flagged — theme splice chains passing content through multiple transformations where each assumes the previous one did the escaping. Recommend a project-wide rule: HTML-escape exactly once, at the moment of interpolation into the HTML template, and never touch escaped output with `.replace`. Additionally, the pattern of "dead helper functions" (`_sparkline_svg`, unused imports) suggests this renderer was built iteratively without cleanup. A linter pass with `ruff --select F401,F841` would flag these.
