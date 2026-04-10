#!/usr/bin/env python3
"""Build a per-file adversarial review prompt for the spiral review.

Usage:
    python audits/spiral/_build_prompt.py <file_path> <ring_number> <ring_description> [orphan_context]

Reads:
    audits/spiral/_prompt_template.md
    audits/spiral/_atlas_focus.md
    <file_path>

Writes:
    audits/spiral/_temp_prompt.md  (overwritten each call)

Then prints the line count to stdout so the caller can record it.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SPIRAL_DIR = REPO_ROOT / "audits" / "spiral"
TEMPLATE = SPIRAL_DIR / "_prompt_template.md"
FOCUS = SPIRAL_DIR / "_atlas_focus.md"
TEMP_OUT = SPIRAL_DIR / "_temp_prompt.md"
DATE = "2026-04-09"


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2

    file_path = Path(sys.argv[1])
    ring_number = sys.argv[2]
    ring_description = sys.argv[3]
    orphan_context = sys.argv[4] if len(sys.argv) > 4 else ""

    if not file_path.is_absolute():
        file_path = (REPO_ROOT / file_path).resolve()

    if not file_path.exists():
        print(f"ERROR: file not found: {file_path}", file=sys.stderr)
        return 1

    file_contents = file_path.read_text(encoding="utf-8", errors="replace")
    line_count = file_contents.count("\n") + (0 if file_contents.endswith("\n") else 1)

    template = TEMPLATE.read_text(encoding="utf-8")
    focus = FOCUS.read_text(encoding="utf-8")

    forward_path = str(file_path).replace("\\", "/")

    prompt = (
        template
        .replace("{{FILE_PATH}}", forward_path)
        .replace("{{FILE_NAME}}", file_path.name)
        .replace("{{RING_NUMBER}}", str(ring_number))
        .replace("{{RING_DESCRIPTION}}", ring_description)
        .replace("{{LINE_COUNT}}", str(line_count))
        .replace("{{DATE}}", DATE)
        .replace("{{ORPHAN_CONTEXT}}", orphan_context)
        .replace("{{ATLAS_SPECIFIC_ATTACK_SURFACE}}", focus)
        .replace("{{FILE_CONTENTS}}", file_contents)
    )

    TEMP_OUT.write_text(prompt, encoding="utf-8")
    print(f"OK file={file_path.name} loc={line_count} prompt_chars={len(prompt)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
