from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import finalize_skill_result


def load_json(path: str) -> dict:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end contract harness for audible-goodreads-deal-scout.",
    )
    parser.add_argument("--prepare-json", required=True)
    parser.add_argument("--runtime-output", required=True)
    parser.add_argument("--expect-status")
    parser.add_argument("--expect-reason")
    args = parser.parse_args(argv)

    result = finalize_skill_result(load_json(args.prepare_json), load_json(args.runtime_output))
    if args.expect_status and result.get("status") != args.expect_status:
        raise SystemExit(
            f"expected status {args.expect_status!r}, got {result.get('status')!r}"
        )
    if args.expect_reason and result.get("reasonCode") != args.expect_reason:
        raise SystemExit(
            f"expected reason {args.expect_reason!r}, got {result.get('reasonCode')!r}"
        )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
