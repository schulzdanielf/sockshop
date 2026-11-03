"""Invoke the platform's Qwen Skill RCA without injecting or changing chaos."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8010")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--output", type=Path, help="Also save the JSON response locally"
    )
    parser.add_argument(
        "--history-campaign-id",
        help="Override the run's experiment ID when locating its history archive",
    )
    parser.add_argument(
        "--possible-fault-type",
        action="append",
        default=[],
        help="Benchmark fault type the diagnosis must choose; repeat for each option.",
    )
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    endpoint = (
        f"{args.api_base.rstrip('/')}/api/runs/"
        f"{quote(args.run_id, safe='')}/skill-rca"
    )
    if args.history_campaign_id:
        endpoint += (
            "?history_campaign_id="
            + quote(args.history_campaign_id, safe="")
        )
    request = Request(
        endpoint,
        data=json.dumps(
            {"possible_fault_types": args.possible_fault_type}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    print(
        f"Investigando {args.run_id} com Skill RCA/Qwen; aguardando resposta...",
        flush=True,
    )
    try:
        with urlopen(request, timeout=args.timeout) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("status") not in {
            "confirmed",
            "inconclusive",
        }:
            raise ValueError("Platform returned an invalid Skill RCA result")
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
    except HTTPError as exc:
        print(
            f"Skill RCA HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}",
            file=sys.stderr,
        )
        return 1
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"Skill RCA failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
