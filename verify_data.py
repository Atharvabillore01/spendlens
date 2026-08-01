#!/usr/bin/env python3
"""Cross-check the numbers the pipeline reports against the raw spreadsheet.

    python verify_data.py                 # all users, offline (deterministic, free)
    python verify_data.py --live          # same, against the real model
    python verify_data.py --json          # machine-readable, for CI
    python verify_data.py --user usr_a1b2c3d4

Every figure is recomputed by `src/observability/verify.py` directly from the
raw columns, with its own grouping logic -- not by calling back into the code
that produced it. A PASS therefore means two independent paths agree; a FAIL
means the answer a user would have seen is wrong.

Exit status is non-zero if any check fails, so this can gate a build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import get_settings  # noqa: E402
from src.observability.verify import verify_result  # noqa: E402
from src.pipeline import TransactionRAGPipeline, load_transactions  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, YELLOW = "\033[32m", "\033[31m", "\033[33m"

# One prompt per chart tool, so every code path that can produce a figure is
# exercised for every user.
PROMPTS = [
    "What did I spend the most on last month?",
    "Show me my spending trend",
    "Am I saving money?",
    "Show me my food spending",
    "Show me my top merchants",
    "Did I spend more in November than October?",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", help="verify a single user id")
    parser.add_argument("--live", action="store_true", help="use the real LLM instead of the scripted one")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")
    args = parser.parse_args()

    raw = pd.read_excel(get_settings().data_path)

    client = None
    if not args.live:
        from demo import offline_client  # noqa: PLC0415

        client = offline_client()

    pipeline = TransactionRAGPipeline(df=load_transactions(), llm_client=client)
    user_ids = [args.user] if args.user else list(pipeline.store.user_ids)

    payload = []
    total_checks = 0
    total_failed = 0

    for user_id in user_ids:
        name = pipeline.store.user_name(user_id)
        if not args.as_json:
            print(f"\n{BOLD}{name}{RESET} {DIM}{user_id}{RESET}")

        for prompt in PROMPTS:
            result = pipeline.run(user_id, prompt)
            reports = verify_result(raw, result)

            if not reports:
                if not args.as_json:
                    print(f"  {DIM}· {prompt}{RESET}\n    {YELLOW}no chart produced — nothing to verify{RESET}")
                continue

            for report in reports:
                checks = len(report.checks)
                failed = len(report.failures)
                total_checks += checks
                total_failed += failed
                payload.append({"prompt": prompt, **report.as_dict()})

                if args.as_json:
                    continue

                mark = f"{GREEN}PASS{RESET}" if report.ok else f"{RED}FAIL{RESET}"
                tool = report.tool.replace("plot_", "")
                print(f"  {mark} {tool:<24} {DIM}{report.period:<16} {checks} checks{RESET}")
                for check in report.failures:
                    print(
                        f"       {RED}✗ {check.name}{RESET} "
                        f"reported={check.reported} recomputed={check.recomputed} "
                        f"delta={check.delta} {DIM}{check.detail}{RESET}"
                    )

    if args.as_json:
        print(json.dumps({"checks": total_checks, "failed": total_failed, "reports": payload}, indent=2))
    else:
        verdict = f"{GREEN}all agree{RESET}" if not total_failed else f"{RED}{total_failed} MISMATCHED{RESET}"
        print(f"\n{BOLD}{total_checks} figures cross-checked against the spreadsheet — {verdict}{RESET}")

    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
