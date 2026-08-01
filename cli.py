#!/usr/bin/env python3
"""Interactive chat loop — the easiest way to see cross-turn cache continuity.

    python cli.py                      # pick a user interactively
    python cli.py --user usr_a1b2c3d4
    python cli.py --offline            # scripted LLM, no network

Commands inside the session:
    :users     list users and switch
    :cache     show this user's three cache entries
    :health    pipeline health
    :flags     guardrail flags from the last turn
    :reset     invalidate this user's cache
    :quit
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import get_settings  # noqa: E402
from src.pipeline import TransactionRAGPipeline, load_transactions  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, YELLOW, BLUE, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[36m"


def choose_user(pipeline: TransactionRAGPipeline) -> str:
    print(f"\n{BOLD}Users{RESET}")
    for i, user_id in enumerate(pipeline.store.user_ids, 1):
        frame = pipeline.store.get_user_frame(user_id)
        print(f"  {i}. {pipeline.store.user_name(user_id):<18} {DIM}{user_id} · {len(frame)} transactions{RESET}")
    while True:
        raw = input(f"{BOLD}Select 1-{len(pipeline.store.user_ids)}: {RESET}").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(pipeline.store.user_ids):
            return pipeline.store.user_ids[int(raw) - 1]
        if raw in pipeline.store.user_ids:
            return raw
        print(f"{RED}Not a valid selection.{RESET}")


def show(result: dict) -> None:
    print()
    for line in textwrap.wrap(result["response"], width=92):
        print(f"  {line}")
    for path in result["visualizations"]:
        print(f"  {BLUE}▸ {path}{RESET}")

    flags = result["guardrail_flags"]
    parts = [
        f"{'HIT' if result['cache_hit'] else 'MISS'}",
        f"{result['latency_ms']}ms",
        result.get("model_used") or "no-llm",
    ]
    if result.get("degraded"):
        parts.append("DEGRADED")
    tail = f"  {DIM}[{' · '.join(parts)}]{RESET}"
    if flags:
        tail += f" {YELLOW}{', '.join(flags)}{RESET}"
    print(tail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", help="user_id to start as")
    parser.add_argument("--offline", action="store_true", help="use a scripted fake LLM (no network)")
    args = parser.parse_args()

    settings = get_settings()
    offline = args.offline or not settings.llm_enabled
    if offline and not args.offline:
        print(f"{YELLOW}OPENROUTER_API_KEY not set — running offline with a scripted LLM.{RESET}")

    client = None
    if offline:
        from demo import offline_client  # noqa: PLC0415

        client = offline_client()

    pipeline = TransactionRAGPipeline(df=load_transactions(), llm_client=client)

    print(f"{BOLD}Transaction RAG Pipeline — interactive{RESET}")
    print(f"{DIM}{'offline (scripted LLM)' if offline else 'live · ' + settings.model_fallback_chain[0]} · "
          f"analysis anchored to {pipeline.store.as_of:%Y-%m-%d} · :quit to exit{RESET}")

    user_id = args.user if args.user and pipeline.store.validate_user(args.user) else choose_user(pipeline)
    last: dict = {}

    while True:
        name = pipeline.store.user_name(user_id)
        try:
            prompt = input(f"\n{BOLD}{CYAN}{name}{RESET} {BOLD}>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not prompt:
            continue
        if prompt in (":quit", ":q", ":exit"):
            return 0
        if prompt == ":users":
            user_id = choose_user(pipeline)
            continue
        if prompt == ":cache":
            print(json.dumps(pipeline.cache_snapshot(user_id), indent=2, default=str))
            continue
        if prompt == ":health":
            print(json.dumps(pipeline.health(), indent=2, default=str))
            continue
        if prompt == ":flags":
            print(f"  {last.get('guardrail_flags', [])}")
            continue
        if prompt == ":reset":
            pipeline.invalidate_user(user_id)
            print(f"  {GREEN}cache cleared for {name}{RESET}")
            continue

        last = pipeline.run(user_id, prompt)
        show(last)


if __name__ == "__main__":
    raise SystemExit(main())
