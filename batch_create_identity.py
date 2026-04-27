#!/usr/bin/env python3
"""
Batch Create Identity - run multiple briefs in parallel.

Wraps create_identity.py so a directory of brief JSON files can be processed
concurrently. Each parallel worker runs a full independent workflow (submit,
poll, promote, poll preprocessing) so there is no shared state between threads.

Each brief should have either:
  - --auto-promote behavior (set in this batch script), in which case the first
    completed draft is promoted automatically, or
  - --pick built into the brief logic (not supported here; use the single-file
    script for interactive picking).

Usage:
    # Process every brief in briefs/
    python batch_create_identity.py \
        --briefs-dir briefs/ \
        --token YOUR_API_TOKEN \
        --output-dir output/

    # Process specific briefs
    python batch_create_identity.py \
        --briefs briefs/scandinavian-model.json briefs/expert-prompt.json \
        --token YOUR_API_TOKEN \
        --output-dir output/ \
        --parallel 3
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from create_identity import CreateIdentity


def process_single_brief(base_url, token, brief_path, output_folder, name=None):
    """Run one full create-and-promote workflow. Always uses --auto-promote in batch mode."""
    start = time.time()

    workflow = CreateIdentity(
        base_url=base_url,
        token=token,
        brief_path=str(brief_path),
        output_folder=str(output_folder),
        auto_promote=True,
        explicit_name=name,
    )

    success = workflow.run()
    elapsed = time.time() - start

    return {
        "brief": brief_path.name,
        "success": success,
        "identity_code": workflow.identity_code,
        "processing_time": round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Batch Create Identity - run multiple briefs in parallel"
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--briefs-dir",
        type=str,
        help="Directory containing brief JSON files (every *.json file is processed)",
    )
    input_group.add_argument(
        "--briefs",
        type=str,
        nargs="+",
        help="Specific brief file paths to process",
    )

    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="API token from https://app.on-model.com/profile?tab=tokens",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="Base output directory (default: output). Each brief's output goes "
             "to <output-dir>/<brief-stem>/.",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://v2.api.piktid.com",
        help="API base URL (default: https://v2.api.piktid.com)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="Number of parallel workers (default: 3, max: 5)",
    )

    args = parser.parse_args()

    parallel = min(max(args.parallel, 1), 5)

    if args.briefs_dir:
        briefs_dir = Path(args.briefs_dir)
        if not briefs_dir.exists():
            print(f"Briefs directory not found: {briefs_dir}")
            exit(1)
        brief_paths = sorted(briefs_dir.glob("*.json"))
    else:
        brief_paths = [Path(p) for p in args.briefs]

    brief_paths = [p for p in brief_paths if p.exists() and p.is_file()]
    if not brief_paths:
        print("No briefs to process")
        exit(1)

    print(f"Running {len(brief_paths)} brief(s) with {parallel} parallel worker(s)")
    print()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    started_at = datetime.utcnow().isoformat() + "Z"

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {
            executor.submit(
                process_single_brief,
                args.base_url,
                args.token,
                brief_path,
                output_dir / brief_path.stem,
            ): brief_path
            for brief_path in brief_paths
        }

        for future in as_completed(futures):
            brief_path = futures[future]
            try:
                result = future.result()
                results.append(result)
                status = "OK" if result["success"] else "FAIL"
                identity = result.get("identity_code") or "-"
                print(f"[{status}] {brief_path.name}  identity={identity}  "
                      f"time={result['processing_time']}s")
            except Exception as e:
                results.append({
                    "brief": brief_path.name,
                    "success": False,
                    "error": str(e),
                })
                print(f"[FAIL] {brief_path.name}  error={e}")

    summary = {
        "started_at": started_at,
        "finished_at": datetime.utcnow().isoformat() + "Z",
        "total_briefs": len(brief_paths),
        "succeeded": sum(1 for r in results if r["success"]),
        "failed": sum(1 for r in results if not r["success"]),
        "results": results,
    }

    summary_path = output_dir / "batch_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print()
    print(f"Summary saved to {summary_path}")
    print(f"Succeeded: {summary['succeeded']} / {summary['total_briefs']}")


if __name__ == "__main__":
    main()
