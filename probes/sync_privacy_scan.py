#!/usr/bin/env python3
"""Vendor the canonical privacy-gate files from a diploid-agent checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "probes" / "privacy-scan-source.json"
CANONICAL_REPOSITORY = "emiltsoi/diploid-agent"
CANONICAL_FILES = (
    "probes/privacy_scan.py",
    "probes/privacy-pre-push",
    "probes/privacy-ci-scan",
    "tests/test_privacy_scan.py",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_head(canonical_root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=canonical_root, text=True
    ).strip()


def sync(canonical_root: Path) -> None:
    missing = [
        relative for relative in CANONICAL_FILES if not (canonical_root / relative).is_file()
    ]
    if missing:
        raise ValueError("canonical checkout is missing: " + ", ".join(missing))

    for relative in CANONICAL_FILES:
        source = canonical_root / relative
        destination = ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    manifest = {
        "schema": "privacy-scan-source/v1",
        "canonical_repository": CANONICAL_REPOSITORY,
        "canonical_commit": canonical_head(canonical_root),
        "files": {relative: sha256_file(ROOT / relative) for relative in CANONICAL_FILES},
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--canonical-root",
        type=Path,
        required=True,
        help="checkout of emiltsoi/diploid-agent at the commit to vendor",
    )
    args = parser.parse_args(argv)

    try:
        sync(args.canonical_root.resolve())
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"privacy scan sync failed: {exc}", file=sys.stderr)
        return 1

    print(f"privacy scan sync: vendored {len(CANONICAL_FILES)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
