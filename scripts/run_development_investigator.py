#!/usr/bin/env python3
"""Run the read-only Investigator against development-visible episodes only."""

from infineq.agents.development_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
