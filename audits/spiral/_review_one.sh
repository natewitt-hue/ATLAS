#!/usr/bin/env bash
# Review one file via Codex.
# Usage: _review_one.sh <file_path> <ring_number> <ring_description> <output_md_path>
#
# Builds the per-file prompt, invokes Codex `task --prompt-file`, captures stdout
# to output_md_path and stderr to audits/spiral/_last_stderr.log.
# Writes a stub `review-failed` doc on non-zero exit or empty stdout.

set -u

FILE_PATH="$1"
RING_NUM="$2"
RING_DESC="$3"
OUT_MD="$4"

COMPANION="C:/Users/natew/.claude/plugins/cache/openai-codex/codex/1.0.2/scripts/codex-companion.mjs"
SPIRAL_DIR="audits/spiral"
PROGRESS="$SPIRAL_DIR/PROGRESS.md"

echo ">>> Reviewing $FILE_PATH -> $OUT_MD"

if ! python "$SPIRAL_DIR/_build_prompt.py" "$FILE_PATH" "$RING_NUM" "$RING_DESC"; then
  echo "!!! prompt build failed for $FILE_PATH"
  exit 1
fi

node "$COMPANION" task --prompt-file "$SPIRAL_DIR/_temp_prompt.md" > "$OUT_MD" 2>"$SPIRAL_DIR/_last_stderr.log"
EXIT=$?

if [ $EXIT -ne 0 ] || [ ! -s "$OUT_MD" ]; then
  echo "!!! codex failed for $FILE_PATH (exit=$EXIT, size=$(stat -c%s "$OUT_MD" 2>/dev/null || echo 0))"
  basename=$(basename "$FILE_PATH")
  cat > "$OUT_MD" <<EOF
# Adversarial Review: $basename

**Verdict:** review-failed
**Ring:** $RING_NUM
**Reviewed:** 2026-04-09
**Error:** Codex exit=$EXIT — see audits/spiral/_last_stderr.log

## Findings

Codex did not return a parseable adversarial review for this file. Inspect _last_stderr.log
for the failure mode and re-invoke manually with:

\`\`\`
python audits/spiral/_build_prompt.py $FILE_PATH $RING_NUM "$RING_DESC"
node "$COMPANION" task --prompt-file audits/spiral/_temp_prompt.md > $OUT_MD
\`\`\`
EOF
  exit 0
fi

# Verify the output looks like a real adversarial review
if ! head -1 "$OUT_MD" | grep -q "^# Adversarial Review:"; then
  echo "!!! malformed output (no H1 marker) for $FILE_PATH"
  # Keep the output but flag it for human review
  TEMP=$(mktemp)
  echo "# Adversarial Review: $(basename $FILE_PATH) [MALFORMED - first line missing H1 marker]" > "$TEMP"
  echo "" >> "$TEMP"
  echo "**Verdict:** needs-human-check" >> "$TEMP"
  echo "" >> "$TEMP"
  echo "Original Codex output below:" >> "$TEMP"
  echo "" >> "$TEMP"
  cat "$OUT_MD" >> "$TEMP"
  mv "$TEMP" "$OUT_MD"
fi

# Mark progress
py_basename=$(basename "$FILE_PATH")
sed -i "s|- \[ \] $py_basename$|- [x] $py_basename|" "$PROGRESS"

loc=$(wc -l < "$OUT_MD")
echo "    -> $loc lines, OK"
