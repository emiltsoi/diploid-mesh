from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "probes" / "privacy-scan-source.json"
CHECK_SCRIPT = ROOT / "probes" / "check_privacy_scan_sync.py"
CANONICAL_FILES = {
    "probes/privacy_scan.py",
    "probes/privacy-pre-push",
    "probes/privacy-ci-scan",
    "tests/test_privacy_scan.py",
}


def test_manifest_declares_expected_canonical_files_and_hashes() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["schema"] == "privacy-scan-source/v1"
    assert manifest["canonical_repository"] == "emiltsoi/diploid-agent"
    assert len(manifest["canonical_commit"]) == 40
    assert set(manifest["files"]) == CANONICAL_FILES
    for relative, expected in manifest["files"].items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected


def test_check_script_passes_against_manifest() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ci_checks_out_the_pinned_canonical_source_and_verifies_vendored_files() -> None:
    import yaml

    data = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = data["jobs"]["test"]["steps"]

    resolve = next(
        step for step in steps if step.get("name") == "Resolve privacy scan canonical commit"
    )
    assert resolve["id"] == "privacy-scan-source"

    canonical_checkout = next(
        step
        for step in steps
        if step.get("uses") == "actions/checkout@v4"
        and step.get("with", {}).get("path") == "privacy-scan-canonical"
    )
    assert canonical_checkout["with"]["repository"] == "emiltsoi/diploid-agent"
    assert canonical_checkout["with"]["ref"] == "${{ steps.privacy-scan-source.outputs.commit }}"

    sync_check = next(step for step in steps if step.get("name") == "Privacy scan sync")
    assert sync_check["run"] == (
        "python probes/check_privacy_scan_sync.py --canonical-root privacy-scan-canonical"
    )
