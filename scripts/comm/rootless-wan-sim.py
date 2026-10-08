#!/usr/bin/env python3
"""Internal entry point; use run_experiment.py --wan-sim rootless-local."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rootless_wan import helper_main

if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Missing internal namespace context; use run_experiment.py")
        status = helper_main(json.loads(sys.argv[1]))
    except (ValueError, KeyError, TypeError) as exc:
        print(f"rootless-local: {exc}", file=sys.stderr)
        status = 1
    raise SystemExit(status)
