#!/usr/bin/env python3
"""Download/resume and validate Omaha inputs outside Blender. Never imports bpy."""

import sys
from pathlib import Path

# Standalone Python resolves this saved entry script, independent of the CWD.
PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def main(argv: list[str] | None = None) -> int:
    import argparse

    from omaha_config import make_config, validate_config
    from omaha_data import prefetch_data

    parser = argparse.ArgumentParser(description=__doc__)
    area = parser.add_mutually_exclusive_group()
    area.add_argument(
        "--full", dest="preview", action="store_false", help="Full metro, 64 tiles (default)."
    )
    area.add_argument(
        "--preview", dest="preview", action="store_true", help="Preview area, 4 tiles."
    )
    parser.set_defaults(preview=False)
    parser.add_argument("--cache-dir", type=Path, help="Override the shared cache directory.")
    parser.add_argument(
        "--check", action="store_true", help="Fully validate local files; never download or write."
    )
    parser.add_argument(
        "--overpass-url",
        action="append",
        metavar="URL",
        help="Endpoint; repeat to supply fallback endpoints. Each actual source is recorded.",
    )
    parser.add_argument("--attempts", type=int, help="Maximum attempts per file (default: 3).")
    parser.add_argument(
        "--timeout", type=float, help="Timeout in seconds per network operation (default: 240)."
    )
    args = parser.parse_args(argv)
    cfg = make_config(preview=args.preview)
    if args.cache_dir is not None:
        cfg["cache_dir"] = str(args.cache_dir.expanduser().resolve())
    if args.overpass_url:
        cfg["overpass_url"] = args.overpass_url[0]
        cfg["overpass_fallback_urls"] = args.overpass_url[1:]
    if args.attempts is not None:
        cfg["download_attempts"] = args.attempts
    if args.timeout is not None:
        cfg["download_timeout"] = args.timeout
    validate_config(cfg)
    print(
        f"Cache: {cfg['cache_dir']}\nAcquisition: sequential; "
        f"{'validation only, no network' if args.check else 'resumable downloads enabled'}",
        flush=True,
    )
    try:
        return 0 if prefetch_data(cfg, check_only=args.check) else 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
