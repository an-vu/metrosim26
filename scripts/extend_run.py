#!/usr/bin/env python3
"""Check or extend saved calculations without Blender or network downloads."""

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def main(argv=None):
    from metrosim26.continuation import extension_config, inspect_extension
    from metrosim26.simulation import prepare_simulation

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Completed run or simulation directory")
    parser.add_argument("--end-year", required=True, type=int)
    parser.add_argument(
        "--check", action="store_true", help="Validate and restore only; write nothing"
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Parent directory for new extension outputs"
    )
    args = parser.parse_args(argv)
    cfg = extension_config(args.source, args.end_year, output_dir=args.output_dir)
    if args.check:
        source, metadata, _, _, resume = inspect_extension(cfg)
        print(f"Source validated: {source}")
        print(f"Will calculate only {metadata['end_year'] + 1}–{cfg['end_year']}")
        print(f"Restored {len(resume['result']['states'][-1]['sites']):,} active sites")
    else:
        _, _, result = prepare_simulation(cfg)
        print(f"Extended calculation saved: {result['run_directory']}")
        print("For Blender replay, keep this original source as EXTEND_FROM, the same END_YEAR")
        print("and output directory, and set RUN_MODE='REPLAY'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
