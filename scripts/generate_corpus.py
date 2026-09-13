"""Command-line entry point for the frozen Infineq v1 corpus generator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from infineq.simulator.generator import generate_corpus


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v1", choices=("v1",))
    parser.add_argument("--output-root", type=Path, default=Path("data/infineq/v1"))
    parser.add_argument("--verify-reproducible", action="store_true")
    args = parser.parse_args()
    summary = generate_corpus(args.output_root, verify_reproducible=args.verify_reproducible)
    if args.verify_reproducible and not summary.reproducible:
        raise SystemExit("observed corpus regeneration was not byte-stable")
    print(json.dumps(summary.to_record(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
