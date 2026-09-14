from __future__ import annotations

import argparse
import logging
import sys

from pipeline.universe_builder import run_universe_search


def cli_progress(stage: str, detail: str) -> None:
    print(f"  [{stage}] {detail}")


def run_one(market: str, country: str, prompt: str, brief: str = "") -> None:
    print(f"\n=== {market} | {country} | {prompt or '(all relevant players)'} ===")
    result = run_universe_search(market, country, prompt, brief=brief, progress_cb=cli_progress)

    print(f"\nCandidates found:   {result.total_candidates_found}")
    print(f"Passed verification: {result.total_verified}")
    print(f"Dropped by category/relevance filter: {result.dropped_by_category}")
    print(f"Final companies:    {len(result.companies)}")
    print(f"Duration:           {result.duration_seconds:.0f}s")
    print("Search engines used:")
    for name, ok in result.engines_used.items():
        print(f"  {name}: {'OK' if ok else 'UNREACHABLE (skipped)'}")
    print("\nOutputs:")
    for fmt, path in result.output_paths.items():
        print(f"  {fmt}: {path}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Market Universe Finder V2 - Universe Mode CLI")
    parser.add_argument("--query", help="Market name, e.g. 'HDPE Jerry Can Market'")
    parser.add_argument("--country", default="global", help="Target geography, e.g. 'Europe'")
    parser.add_argument("--prompt", default="", help="Category to output, e.g. 'Parent Companies' (leave empty for all relevant players)")
    parser.add_argument("--brief", help="Detailed free-text scope brief with inclusion/exclusion rules")
    parser.add_argument("--brief-file", help="Path to a file containing the detailed brief")
    parser.add_argument("--file", help="Batch mode: file with one 'Market | Country | Prompt' per line")
    args = parser.parse_args()

    brief = args.brief or ""
    if args.brief_file:
        with open(args.brief_file, encoding="utf-8") as f:
            brief = f.read()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) != 3:
                    print(f"Skipping malformed line: {line}")
                    continue
                run_one(*parts)
        return

    if args.query:
        run_one(args.query, args.country, args.prompt, brief)
        return

    # interactive loop
    print("Market Universe Finder V2 - interactive mode (type 'quit' to exit)")
    while True:
        market = input("\nMarket name: ").strip()
        if market.lower() == "quit":
            break
        country = input("Geography [global]: ").strip() or "global"
        prompt = input("Category prompt (blank = all relevant players): ").strip()
        run_one(market, country, prompt)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
