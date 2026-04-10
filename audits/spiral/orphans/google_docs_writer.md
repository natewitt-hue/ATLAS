# Adversarial Review: google_docs_writer.py

**Verdict:** needs-attention
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 266
**Reviewer:** Claude (delegated subagent)
**Total findings:** 12 (2 critical, 5 warnings, 5 observations)

**ORPHAN STATUS: LIVE**
This file is not imported through bot.py's direct dependency chain but IS imported by active code: `cortex/cortex_main.py` (sibling project, not the main Discord bot). Argus's static scan missed it because the bot.py spiral doesn't trace through sibling projects. Review as active code, but note its blast radius is scoped to the cortex subsystem, not the main ATLAS bot.

## Summary

Google Docs writer with synchronous blocking I/O, an interactive `flow.run_local_server(port=0)` auth bootstrap that can never complete in a long-running bot, an unescaped Drive query that is vulnerable to injection on the folder name, and a non-atomic document creation sequence that can orphan empty docs in the user's Drive on any partial failure. Because cortex is a sibling automation, the blast radius is a reports folder rather than user-facing Discord flows — but the auth + atomicity risks are real and hard to detect when they fire.

## Findings

### CRITICAL #1: `get_or_create_folder` passes an unquoted folder name directly into the Drive `q` query — folder name injection
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:45-67`
**Confidence:** 0.90
**Risk:** The query is `f"name = '{folder_name}' and mimeType = ..."`. If `folder_name` contains a single quote — e.g., `"Nate's Reports"` or worse, `"x' or name = 'ORACLE Reports"` — the query either raises a syntax error (best case) or matches a different folder than intended (worst case). Google's Drive query language supports boolean operators, so a malicious or accidental folder name like `"x' or name='Admin"` could find and return the Admin folder's ID.
**Vulnerability:** `save_to_google_docs(...)` accepts `folder_name: str = "ORACLE Reports"` as a parameter. If any caller passes user-supplied text (which is a reasonable future feature: "save to custom folder"), this is a direct injection vector. Even if the default is hardcoded, an apostrophe in the name — common in "Nate's Drive" scenarios — trips the same bug.
**Impact:** Documents may be created in the wrong folder, or the folder-lookup step raises an opaque API error that aborts the whole save. Potential cross-user data leakage if the workspace has multiple named folders.
**Fix:** Use `folder_name.replace("\\", "\\\\").replace("'", "\\'")` to escape the value, OR use parameterized queries via the Drive API's `q` field (Google's API supports `\'` as an escape). Reject any control characters.

### CRITICAL #2: Document creation + folder move + content write are three non-atomic API calls with no rollback on partial failure
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:219-266`
**Confidence:** 0.85
**Risk:** Sequence:
1. `docs_service.documents().create(...)` creates an empty doc in the root of the user's Drive (line 239).
2. `drive_service.files().update(addParents=folder_id, removeParents="root", ...)` moves it into the ORACLE Reports folder (lines 243-248).
3. `docs_service.documents().batchUpdate(body={"requests": requests})` writes the formatting (lines 257-260).

If step 2 raises (permission error on root removal, folder deleted between steps 1 and 2, network timeout), the new blank doc is stranded at the root of the user's Drive with no cleanup. If step 3 raises (any batchUpdate failure — the batch can fail if even one style request is malformed), the doc exists in the folder but is empty. On repeated failures, the user accumulates a collection of blank orphan docs named "Clinical Profile — X — 2026-04-09".
**Vulnerability:** No try/except around the three-step sequence, no cleanup logic, no idempotency. A retry of `save_to_google_docs` will create another doc, not recover the previous one.
**Impact:** Blank/orphan docs accumulate in the user's Drive. On a busy retry loop, the Drive can fill up with hundreds of junk docs. No way to detect except manually auditing Drive.
**Fix:** Wrap steps 2 and 3 in `try: ... except: docs_service.documents().delete(doc_id); raise`. Or use a pending-state file naming convention (`pending_...`) that is renamed after success.

### WARNING #1: `get_google_credentials` calls `InstalledAppFlow.from_client_secrets_file(...).run_local_server(port=0)` — interactive browser auth cannot work from a headless bot/server
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:20-42`
**Confidence:** 0.90
**Risk:** On first run (or when `token.json` is missing/invalid), this call opens a local web server on a random port and spawns a browser for the user to complete OAuth. For a Discord bot running in a daemonized process (Linux, Docker, Windows service), `webbrowser.open()` has nothing to open against and the call hangs or fails with `BrowserError`. For a headless VPS deployment (per CLAUDE.md's memory of "FROGLAUNCH — deploy ATLAS to VPS with systemd"), this is a guaranteed failure.
**Vulnerability:** There is no fallback to a service account, no support for a manual auth code, no error handling. If token refresh fails on a deployed bot, the next `save_to_google_docs` call blocks forever or crashes.
**Impact:** On every token expiry on a headless deployment, cortex reports stop being written until someone SSHes in and completes the auth flow.
**Fix:** Use service account credentials (`google.oauth2.service_account.Credentials`) for deployed contexts. Fall back to `run_console()` for manual code entry if browser is unavailable. Raise a clear error with instructions if interactive auth is needed on a headless host.

### WARNING #2: `token.json` and `credentials.json` are loaded from the working directory with no validation — and `token.json` is written with world-readable default perms
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:16-17, 39-40`
**Confidence:** 0.80
**Risk:** `TOKEN_PATH = "token.json"` resolves to the current working directory — not an absolute path, not an XDG-compliant location. If the bot is started from a different directory (or the CWD changes mid-process), the file may not be found. More importantly, `with open(TOKEN_PATH, "w")` at line 39 writes the refresh token to disk with the process's default umask — on most Linux systems that's 0644 (world-readable). A refresh token is effectively a long-lived credential that grants "drive.file + documents" scope.
**Vulnerability:** Any other process or user on the box can read `token.json` and impersonate the bot against Google. Additionally, if the bot is run from `~/Desktop/discord_bot/` one day and `/opt/atlas/` the next, you end up with two stale tokens in two different directories.
**Impact:** Refresh token exfiltration on shared hosts. Lost-token confusion on deployment.
**Fix:** Use absolute paths resolved from `$ATLAS_CONFIG_DIR` env var. Explicitly chmod the written token file to `0600`. Refuse to run if the file already exists with permissive permissions.

### WARNING #3: `batchUpdate` request list is built iteratively with `insert_index` that assumes UTF-8 character == 1 index — fails for emoji, combining marks, surrogate pairs
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:142-173`
**Confidence:** 0.70
**Risk:** `length = len(content)` uses Python's string length, which counts code points. Google Docs' API counts UTF-16 code units for its index arithmetic. For most ASCII text they match. For emoji (e.g., 🔮, 🏈 that appear elsewhere in the codebase), a single Python character can be 2 UTF-16 code units. The result: `insert_index` drifts after the first emoji, causing subsequent `updateTextStyle` ranges to slice into the wrong part of the document.
**Vulnerability:** If a report ever contains an emoji (possible via ATLAS Oracle output), the resulting doc has garbled styling or the batch update raises "index out of bounds."
**Impact:** Corrupted or mis-styled reports when any emoji or non-BMP character is present.
**Fix:** Use `len(content.encode("utf-16-le")) // 2` to compute the index advance, matching Google's counting. Or: sanitize the content to strip non-BMP characters before inserting.

### WARNING #4: `insert_text` sends 3 API requests per call — inflated request count can trip quota on large reports
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:142-173`
**Confidence:** 0.65
**Risk:** Every text insertion produces `insertText + updateParagraphStyle + updateTextStyle` = 3 requests. A 200-line report means 600+ requests in one `batchUpdate`. Google's batch endpoint has an undocumented cap around ~500-1000 operations depending on size. On a long report, the batch fails with HTTP 400 "too many requests in batch" after having already created the doc.
**Vulnerability:** Combined with the non-atomic creation path above, a long report leaves a blank doc in Drive.
**Impact:** Large reports fail with partial state; short reports work fine. Intermittent and hard to reproduce.
**Fix:** Chunk `requests` into batches of ≤200 and call `batchUpdate` multiple times. Track how many batches succeeded so a failure can roll back.

### WARNING #5: `parse_report_into_sections` silently drops every blank line — multi-paragraph body text collapses
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:70-100`
**Confidence:** 0.75
**Risk:** `if not stripped: continue` at line 82 drops any empty line. This destroys paragraph breaks in body text: the input

```
First paragraph.

Second paragraph.
```

becomes two consecutive `("body", ...)` elements rendered with no space between them. The renderer at `build_requests` adds newlines per-element but the paragraph style is `NORMAL_TEXT` so there's no visual separation.
**Vulnerability:** Reports lose their paragraph structure entirely. The author may not notice during testing if all test inputs are single-paragraph.
**Impact:** Oracle reports produced for the cortex subsystem display as wall-of-text in Google Docs.
**Fix:** When encountering a blank line inside body content, emit a `("body", "")` spacer element or a paragraph break.

### OBSERVATION #1: Regex at line 91 checks `re.match(r'^\*[^*]+\*$', stripped)` — correct for single-star italic lines but silently fails if the line has trailing whitespace before the closing asterisk
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:91-94`
**Confidence:** 0.50
**Risk:** The regex requires exact `*text*` with no interior whitespace at edges. `*hello world *` (trailing space) fails and falls to the body branch. Minor edge case but the comment claims "avoids matching lines that merely contain asterisks mid-text" — it actually also avoids matching perfectly valid italic lines with trailing whitespace.
**Fix:** `^\*\s*([^*]+?)\s*\*$` or strip whitespace before matching.

### OBSERVATION #2: `print(...)` calls throughout instead of `log.info/log.debug`
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:57, 66, 224, 231, 238, 251, 263-264`
**Confidence:** 0.95
**Risk:** Six `print` calls scattered through the save flow. No logger is used. Output goes to stdout instead of the structured logging system used elsewhere in ATLAS.
**Fix:** Import `logging` and use `log = logging.getLogger(__name__)` / `log.info(...)`.

### OBSERVATION #3: No retry on transient API errors
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:239-260`
**Confidence:** 0.85
**Risk:** Google APIs rate-limit and return transient 429/503 errors. A single API call failure (common under load) aborts the entire save with no backoff.
**Fix:** Wrap API calls in `tenacity`-style exponential backoff for 429/503 responses.

### OBSERVATION #4: `doc_title` is built via f-string with a subject that could contain slashes or other Drive-hostile characters
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:235-236`
**Confidence:** 0.55
**Risk:** `f"Clinical Profile — {subject} — {date_str}"` — if `subject` contains a slash or other Drive-restricted character, the create call either fails or sanitizes silently.
**Fix:** Sanitize subject with `re.sub(r'[<>:"/\\|?*]', '-', subject)` or document the contract.

### OBSERVATION #5: Module has no `if __name__ == "__main__":` guard, no unit test hooks, no docstring at top of file
**Location:** `C:/Users/natew/Desktop/discord_bot/google_docs_writer.py:1-18`
**Confidence:** 0.40
**Risk:** Style and maintainability nit.
**Fix:** Add a module docstring explaining the contract and scope.

## Cross-cutting Notes

This file is sibling-project code (cortex) rather than ATLAS main. The CLAUDE.md rules around `flow_wallet` / `atlas_ai.generate()` / `get_persona()` do not apply directly here. However, the broader ATLAS hygiene rules — no `print` where `log` is available, avoid blocking I/O patterns that lock up a single-threaded runtime, use service accounts for headless deployments — do apply. If FROGLAUNCH (deploy to VPS) ever lands, the `run_local_server(port=0)` path is a deployment blocker for this file.
