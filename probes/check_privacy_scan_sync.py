#!/usr/bin/env python3
"""Check vendored privacy-gate files against their canonical-source manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "probes" / "privacy-scan-source.json"
EXPECTED_REPOSITORY = "emiltsoi/diploid-agent"
EXPECTED_SCHEMA = "privacy-scan-source/v1"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {MANIFEST_PATH}: {exc}") from exc


def validate_manifest(manifest: dict) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema") != EXPECTED_SCHEMA:
        errors.append(f"schema must be {EXPECTED_SCHEMA}")
    if manifest.get("canonical_repository") != EXPECTED_REPOSITORY:
        errors.append(f"canonical_repository must be {EXPECTED_REPOSITORY}")
    commit = manifest.get("canonical_commit", "")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        errors.append("canonical_commit must be a full lowercase commit SHA")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        errors.append("files must be a non-empty path-to-sha256 mapping")
    elif any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in files.values()
    ):
        errors.append("every files value must be a lowercase sha256 digest")
    return errors


def check_local_files(manifest: dict) -> list[str]:
    errors: list[str] = []
    for relative, expected in sorted(manifest["files"].items()):
        path = ROOT / relative
        if not path.is_file():
            errors.append(f"missing vendored file: {relative}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            errors.append(f"sha256 mismatch for {relative}: manifest={expected} actual={actual}")
    return errors


def check_canonical(manifest: dict, canonical_root: Path) -> list[str]:
    errors: list[str] = []
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=canonical_root, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        return [f"cannot resolve canonical checkout HEAD at {canonical_root}: {exc}"]

    expected_commit = manifest["canonical_commit"]
    if head != expected_commit:
        errors.append(f"canonical checkout is {head}, expected {expected_commit}")

    for relative in sorted(manifest["files"]):
        canonical_path = canonical_root / relative
        vendored_path = ROOT / relative
        if not canonical_path.is_file():
            errors.append(f"canonical file missing: {relative}")
            continue
        if not vendored_path.is_file():
            continue
        if canonical_path.read_bytes() != vendored_path.read_bytes():
            errors.append(f"vendored file differs from canonical source: {relative}")
        canonical_executable = bool(canonical_path.stat().st_mode & 0o111)
        vendored_executable = bool(vendored_path.stat().st_mode & 0o111)
        if canonical_executable != vendored_executable:
            errors.append(f"executable mode differs from canonical source: {relative}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--canonical-root",
        type=Path,
        help="optional checkout of the canonical repository for byte comparison",
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest()
    except ValueError as exc:
        print(f"privacy scan sync: {exc}", file=sys.stderr)
        return 1

    errors = validate_manifest(manifest)
    if not errors:
        errors.extend(check_local_files(manifest))
    if not errors and args.canonical_root is not None:
        errors.extend(check_canonical(manifest, args.canonical_root.resolve()))

    if errors:
        print(f"privacy scan sync: {len(errors)} finding(s)", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("privacy scan sync: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
