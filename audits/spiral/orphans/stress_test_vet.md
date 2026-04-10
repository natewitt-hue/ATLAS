# Adversarial Review: stress_test_vet.py

**Verdict:** LIVE (CLI script — keep but harden)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 62
**Reviewer:** Claude (delegated subagent)
**Total findings:** 4 (0 critical, 1 warning, 3 observations)

## Summary

Smallest of the stress-test family. 10 TSL questions through `atlas_ai.generate()` for accuracy vetting. Has `if __name__ == "__main__":` and is run as `python stress_test_vet.py`. Not dead. The word-wrap loop is so convoluted it's likely buggy, and there's no automated assertion — the script just prints answers for human review.

## Findings

### WARNING #1: Word-wrap loop is logically broken on the first word
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_vet.py:48-58`
**Confidence:** 0.9
**Risk:** The wrap logic is:
```
line = "│   "
for w in words:
    if len(line) + len(w) + 1 > 78:
        print(line)
        line = "│   " + w
    else:
        line += " " + w if line.strip() != "" and line != "│   " else w if line == "│   " else line + " " + w
```
The conditional expression on the inner else-branch is malformed nested ternary that's nearly impossible to read. The `else w if line == "│   " else line + " " + w` is parsed as `else (w if line == "│   " else (line + " " + w))`, which means when `line` is the initial `"│   "`, you do `line += w` (no space between prefix and first word — that's the intent). But the OUTER condition `line.strip() != "" and line != "│   "` checks the OPPOSITE: if line strips to non-empty AND line isn't initial, you append `" " + w`. The chained ternary is doing roughly what the author wanted but in an obscure way that will break the moment anyone touches it.
**Vulnerability:** Logic is correct by accident. Maintenance nightmare.
**Impact:** Future edits will introduce off-by-one wrap bugs in vet output.
**Fix:** Use `textwrap.fill(text, width=78, initial_indent="│   ", subsequent_indent="│   ")` like `stress_test_history.py` already does.

### OBSERVATION #1: Dead-candidate but live CLI
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_vet.py:61-62`
**Confidence:** 0.95
**Risk:** Has main entrypoint, undocumented.
**Fix:** Move to `scripts/tests/` or document.

### OBSERVATION #2: No automated pass/fail signal
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_vet.py:36-58`
**Confidence:** 0.7
**Risk:** Vet test is purely visual — operator must read every answer and judge accuracy. No regression detection. If `atlas_ai.generate()` regresses (e.g. provider switch produces less accurate output), the script will silently print the new answers and the operator may not notice.
**Impact:** Silent regression.
**Fix:** At least add a baseline JSON of expected key facts per question and check that answers contain them (e.g. "the answer about devTrait values must mention 'Star' and 'Superstar'").

### OBSERVATION #3: `system` parameter is always None — dead branch
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_vet.py:18-29, 36-43`
**Confidence:** 0.75
**Risk:** Every question tuple has `None` as the second element (the system prompt slot). The `system=system or ""` then always passes empty string. The system slot is decorative.
**Impact:** Either intent was to populate system prompts later (TODO), or the slot should be removed.
**Fix:** Either populate at least a few entries with system prompts or remove the slot.

## Cross-cutting Notes

This file is the most stripped-down of the stress test family — confirms my recommendation to extract a shared harness. None of the four scripts pull from a common base; each reinvents loop, output, env loading. The cumulative cost of maintaining four near-identical scripts is high.
