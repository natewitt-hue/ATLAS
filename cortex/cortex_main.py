"""
cortex_main.py - ATLAS Cortex CLI
===================================
Standalone entry point for the Cortex cognitive intelligence pipeline.
Zero dependency on Oracle. Runs completely independently.

Usage:
  python cortex_main.py run <nickname>
  python cortex_main.py run <nickname> --no-cache
  python cortex_main.py run <nickname> --no-docs
  python cortex_main.py diagnose <nickname>
  python cortex_main.py export <nickname>

Environment variables required (same as Oracle, from .env):
  GEMINI_API_KEY   - Google Gemini API key
  ORACLE_DB_PATH   - Path to TSL_Archive.db (shared with Oracle)

Optional:
  Google Docs export requires credentials.json (OAuth2 Desktop App)
  from Google Cloud Console. Same credentials.json used by Oracle.
  Output folder in Drive: "CORTEX Reports"
"""

import os
import argparse
import random
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from cortex_engine  import CortexEngine
from cortex_analyst import CortexAnalyst
from cortex_writer  import CortexWriter

# Reuse google_docs_writer from Oracle -- it is generic and folder-name parameterized
try:
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from google_docs_writer import save_to_google_docs
    _docs_available = True
except ImportError:
    _docs_available = False


BANNER = """
+======================================================+
|           ATLAS CORTEX -- INTELLIGENCE PIPELINE       |
|           Cognitive Assessment & Fact-Check Module   |
+======================================================+
"""


def cmd_run(args):
    nickname     = args.nickname
    author_id    = getattr(args, 'id', None)
    skip_cache   = args.no_cache
    skip_docs    = args.no_docs

    print(BANNER)

    if skip_cache:
        _clear_cache()

    # Verify DB exists before starting
    db_path = os.getenv("ORACLE_DB_PATH", "TSL_Archive.db")
    if not os.path.exists(db_path):
        print(f"[-] Database not found: {db_path}")
        print("    Set ORACLE_DB_PATH in .env to the correct path.")
        return

    engine  = CortexEngine()
    analyst = CortexAnalyst()
    writer  = CortexWriter()

    # -- PHASE 1: Evidence Packs -----------------------------------------------
    print("--- PHASE 1: EVIDENCE COLLECTION ---")
    packs = engine.get_evidence_packs(nickname, author_id=author_id)
    if not packs:
        print(f"[-] No messages found for '{nickname}'. Exiting.")
        return

    # -- PHASE 2: Signal Extraction (Flash) -----------------------------------
    stats = packs["cognitive"]["stats"]
    print(f"    Total messages  : {stats.get('total_messages', 0):,}")
    print(f"    Long (300+)     : {stats.get('long_count', 0):,}")
    print(f"    Medium (81-300) : {stats.get('medium_count', 0):,}")
    print(f"    Chains found    : {stats.get('chains_total', 0):,}")
    print(f"    Tone sample     : {len(packs.get('tone', []))}")
    print(f"    Fact check pool : {len(packs.get('fact_check', []))}")

    print("\n--- PHASE 2: SIGNAL EXTRACTION (Flash) ---")
    try:
        signals = analyst.run_all_passes(nickname, packs)
    except Exception as e:
        err_name = type(e).__name__
        print("")
        print(f"[-] PHASE 2 FAILED: {err_name}")
        if "RetryError" in err_name or "TransportError" in err_name:
            print("    The Gemini API server is not responding after multiple retries.")
            print("    This is usually temporary. Wait 1-2 minutes and try again.")
            print("    If it persists, check https://status.cloud.google.com for outages.")
        else:
            print(f"    Error details: {e}")
        return

    # Save raw signals for debugging
    safe_name_json = "".join(c for c in nickname if c.isalnum())
    writer_temp = CortexWriter()
    json_path = writer_temp.save_json_signals(
        signals, f"Cortex_Signals_{safe_name_json}.json", subfolder=safe_name_json
    )
    print(f"[+] Raw signals saved: {json_path}")

    # -- PHASE 3: Report Synthesis (Pro) --------------------------------------
    print("\n--- PHASE 3: REPORT SYNTHESIS (Pro) ---")
    report_text = writer.write_report(nickname, signals)

    # -- PHASE 4: Save Outputs -------------------------------------------------
    print("\n--- PHASE 4: SAVING OUTPUTS ---")
    safe_name  = "".join(c for c in nickname if c.isalnum())
    local_path = writer.save_markdown(
        report_text, f"Cortex_Intelligence_{safe_name}.md", subfolder=safe_name
    )
    print(f"[+] Local backup: {local_path}")

    if not skip_docs:
        if not _docs_available:
            print("[-] Google Docs export unavailable -- google_docs_writer.py not found.")
            print(f"    Report saved locally at: {local_path}")
        else:
            print("\n--- PHASE 5: GOOGLE DOCS EXPORT ---")
            try:
                doc_url = save_to_google_docs(
                    report_text, nickname, folder_name="CORTEX Reports"
                )
                _print_complete(nickname, local_path, doc_url)
            except FileNotFoundError as e:
                print(f"[-] Google Docs export failed: {e}")
                print(f"    Report saved locally at: {local_path}")
            except Exception as e:
                print(f"[-] Google Docs export failed unexpectedly: {e}")
                print(f"    Report saved locally at: {local_path}")
    else:
        _print_complete(nickname, local_path, None)



CHAIN_GAP = 90  # seconds -- same as cortex_engine

SIZE_CAPS = {
    "claude": 700_000,
    "gemini": 3_200_000,
    "full":   0,
}


def _merge_chains(msgs):
    """Merge rapid-fire messages (<=90s gap) into blocks."""
    blocks = []
    current = None

    for m in msgs:
        ts = 0
        try:
            ts = float(m.get("timestamp_unix", 0))
        except (ValueError, TypeError):
            pass

        if current is None:
            current = {
                "id": m.get("message_id", "?"),
                "ts": ts,
                "texts": [str(m.get("content", "")).strip()],
                "count": 1,
            }
            continue

        gap = ts - current["ts_last"] if "ts_last" in current else ts - current["ts"]
        if 0 <= gap <= CHAIN_GAP:
            current["texts"].append(str(m.get("content", "")).strip())
            current["count"] += 1
            current["ts_last"] = ts
        else:
            blocks.append(current)
            current = {
                "id": m.get("message_id", "?"),
                "ts": ts,
                "texts": [str(m.get("content", "")).strip()],
                "count": 1,
            }
        current["ts_last"] = ts

    if current:
        blocks.append(current)

    return blocks


def _format_block(block):
    """Format a merged block as plain text."""
    mid = block["id"]
    try:
        dt = datetime.fromtimestamp(block["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        dt = "unknown"

    count = block["count"]
    header = f"[ID: {mid}] [{dt}]"
    if count > 1:
        header += f" [{count} msgs]"

    body = chr(10).join(block["texts"])
    return header + chr(10) + body + chr(10)


def _stratified_sample(blocks, max_bytes):
    """Sample blocks evenly across the timeline to fit under max_bytes."""
    # Format all blocks and compute sizes
    formatted = []
    for b in blocks:
        text = _format_block(b)
        formatted.append((b, text, len(text.encode("utf-8")) + 1))  # +1 for separator newline

    total_size = sum(s for _, _, s in formatted)
    if max_bytes <= 0 or total_size <= max_bytes:
        return formatted

    # Divide into 100 time slices, sample proportionally from each
    n_slices = 100
    slice_size = len(formatted) // n_slices or 1
    slices = []
    for i in range(0, len(formatted), slice_size):
        slices.append(formatted[i:i + slice_size])

    # Target per slice
    target_per_slice = max_bytes // len(slices)

    sampled = []
    remaining_budget = max_bytes
    for sl in slices:
        slice_items = []
        slice_budget = min(target_per_slice, remaining_budget)
        used = 0
        # Shuffle within slice to avoid always picking first items
        indices = list(range(len(sl)))
        random.shuffle(indices)
        for idx in indices:
            b, text, size = sl[idx]
            if used + size <= slice_budget:
                slice_items.append((b, text, size))
                used += size
        # Sort back to chronological within this slice
        slice_items.sort(key=lambda x: x[0]["ts"])
        sampled.extend(slice_items)
        remaining_budget -= used

    return sampled


def cmd_export(args):
    """Export chain-merged messages sized for a specific LLM target."""
    nickname = args.nickname
    author_id = getattr(args, 'id', None)
    target = args.target
    print(BANNER)
    id_note = f" [id: {author_id}]" if author_id else ""
    print(f"[Cortex] Exporting messages for: {nickname}{id_note} (target: {target})")

    db_path = os.getenv("ORACLE_DB_PATH", "TSL_Archive.db")
    if not os.path.exists(db_path):
        print(f"[-] Database not found: {db_path}")
        return

    engine = CortexEngine()
    msgs = engine.get_all_messages(nickname, author_id=author_id)
    if not msgs:
        print(f"[-] No messages found for '{nickname}'.")
        return

    total_msgs = len(msgs)
    print(f"    -> {total_msgs:,} messages retrieved")

    # Chain merge
    blocks = _merge_chains(msgs)
    chain_blocks = sum(1 for b in blocks if b["count"] > 1)
    standalone = len(blocks) - chain_blocks
    print(f"    -> {len(blocks):,} blocks after chain merge ({chain_blocks:,} chains + {standalone:,} standalone)")

    # Stratified sample to fit target
    max_bytes = SIZE_CAPS.get(target, 0)
    sampled = _stratified_sample(blocks, max_bytes)

    # Write output — per-username subfolder
    safe_name = "".join(c for c in nickname if c.isalnum())
    out_dir = os.path.join("output", "cortex", safe_name)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"Cortex_Export_{safe_name}_{target}.txt")

    with open(out_path, "w", encoding="utf-8") as f:
        for _, text, _ in sampled:
            f.write(text + chr(10))

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    msg_count = sum(b["count"] for b, _, _ in sampled)
    print(f"    -> {len(sampled):,} blocks written ({msg_count:,} messages)")
    print(f"[+] Exported to: {out_path}")
    print(f"    File size: {size_mb:.2f} MB")
    if max_bytes > 0:
        print(f"    Target cap: {max_bytes / 1024 / 1024:.1f} MB")


def cmd_diagnose(args):
    """Run engine diagnostics -- verify DB connection and sample counts."""
    print(BANNER)
    author_id = getattr(args, 'id', None)
    print(f"[Cortex] Running diagnostics for: {args.nickname}")
    engine = CortexEngine()
    packs  = engine.get_evidence_packs(args.nickname, author_id=author_id)
    if packs:
        stats = packs["cognitive"]["stats"]
        print(f"\n[Diagnostics Complete]")
        print(f"  DB path                   : {os.getenv('ORACLE_DB_PATH', 'TSL_Archive.db')}")
        print(f"  Total messages in archive : {stats['total_messages']:,}")
        print(f"  Short  (<=80 chars)       : {stats['short_count']:,}")
        print(f"  Medium (81-300 chars)     : {stats['medium_count']:,}")
        print(f"  Long   (300+ chars)       : {stats['long_count']:,}")
        print(f"  Chains found              : {stats['chains_total']:,}")
        print(f"  Chains sampled            : {stats['chains_sampled']:,}")
        print(f"  Broad vocab sample        : {len(packs['cognitive']['broad_sample']):,}")
        print(f"  Peak candidates           : {len(packs['cognitive']['peak_candidates']):,}")
        print(f"  Fact check pool           : {len(packs['fact_check']):,}")
        print(f"  Tone sample               : {len(packs['tone']):,}")
        print(f"\n  Ready to run: python cortex_main.py run {args.nickname}")


def _clear_cache():
    import shutil
    cache_dir = ".cortex_cache"
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)
        os.makedirs(cache_dir)
        print(f"[+] Cache cleared: {cache_dir}")
    else:
        print(f"[*] No cache to clear.")


def _print_complete(nickname, local_path, doc_url):
    print(f"\n{'='*55}")
    print(f"  ATLAS CORTEX -- COMPLETE")
    print(f"  Subject    : {nickname}")
    print(f"  Local file : {local_path}")
    if doc_url:
        print(f"  Google Doc : {doc_url}")
    print(f"{'='*55}\n")


def main():
    if not os.getenv("GEMINI_API_KEY"):
        print("[-] GEMINI_API_KEY not set in .env -- cannot run.")
        return

    parser = argparse.ArgumentParser(
        description="ATLAS Cortex -- Cognitive Intelligence Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cortex_main.py run TheWitt --id 322498632542846987
  python cortex_main.py run TheWitt --no-cache
  python cortex_main.py export JT --id 871448457414598737 --target full
  python cortex_main.py export JT --id 871448457414598737 --target claude
  python cortex_main.py diagnose TheWitt --id 322498632542846987
        """
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    p_run = subparsers.add_parser("run", help="Run full Cortex intelligence assessment")
    p_run.add_argument("nickname",  help="Display name / folder name for the subject")
    p_run.add_argument("--id", help="Discord snowflake ID (exact match, preferred over nickname)")
    p_run.add_argument("--no-cache", action="store_true",
                       help="Clear .cortex_cache and run all passes fresh")
    p_run.add_argument("--no-docs",  action="store_true",
                       help="Skip Google Docs export (local markdown only)")
    p_run.set_defaults(func=cmd_run)

    # export command
    p_exp = subparsers.add_parser("export", help="Export chain-merged messages sized for LLM input")
    p_exp.add_argument("nickname", help="Display name / folder name for the subject")
    p_exp.add_argument("--id", help="Discord snowflake ID (exact match, preferred over nickname)")
    p_exp.add_argument("--target", choices=["claude", "gemini", "full"], default="gemini",
                       help="Target LLM size cap (claude=700KB, gemini=3.2MB, full=no cap)")
    p_exp.set_defaults(func=cmd_export)

    # diagnose command
    p_diag = subparsers.add_parser("diagnose", help="Check DB connection and sample counts")
    p_diag.add_argument("nickname", help="Display name / folder name for the subject")
    p_diag.add_argument("--id", help="Discord snowflake ID (exact match, preferred over nickname)")
    p_diag.set_defaults(func=cmd_diagnose)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
