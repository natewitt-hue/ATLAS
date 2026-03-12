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
    packs = engine.get_evidence_packs(nickname)
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
    signals = analyst.run_all_passes(nickname, packs)

    # Save raw signals for debugging
    safe_name_json = "".join(c for c in nickname if c.isalnum())
    writer_temp = CortexWriter()
    json_path = writer_temp.save_json_signals(
        signals, f"Cortex_Signals_{safe_name_json}.json"
    )
    print(f"[+] Raw signals saved: {json_path}")

    # -- PHASE 3: Report Synthesis (Pro) --------------------------------------
    print("\n--- PHASE 3: REPORT SYNTHESIS (Pro) ---")
    report_text = writer.write_report(nickname, signals)

    # -- PHASE 4: Save Outputs -------------------------------------------------
    print("\n--- PHASE 4: SAVING OUTPUTS ---")
    safe_name  = "".join(c for c in nickname if c.isalnum())
    local_path = writer.save_markdown(
        report_text, f"Cortex_Intelligence_{safe_name}.md"
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


def cmd_diagnose(args):
    """Run engine diagnostics -- verify DB connection and sample counts."""
    print(BANNER)
    print(f"[Cortex] Running diagnostics for: {args.nickname}")
    engine = CortexEngine()
    packs  = engine.get_evidence_packs(args.nickname)
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
  python cortex_main.py run TheWitt
  python cortex_main.py run TheWitt --no-cache
  python cortex_main.py run TheWitt --no-docs
  python cortex_main.py diagnose TheWitt
        """
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    p_run = subparsers.add_parser("run", help="Run full Cortex intelligence assessment")
    p_run.add_argument("nickname",  help="Subject's nickname as stored in the message DB")
    p_run.add_argument("--no-cache", action="store_true",
                       help="Clear .cortex_cache and run all passes fresh")
    p_run.add_argument("--no-docs",  action="store_true",
                       help="Skip Google Docs export (local markdown only)")
    p_run.set_defaults(func=cmd_run)

    # diagnose command
    p_diag = subparsers.add_parser("diagnose", help="Check DB connection and sample counts")
    p_diag.add_argument("nickname", help="Subject's nickname to diagnose")
    p_diag.set_defaults(func=cmd_diagnose)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
