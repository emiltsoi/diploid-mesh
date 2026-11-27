from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = ROOT / "probes" / "privacy_scan.py"


def load_scanner():
    spec = importlib.util.spec_from_file_location("privacy_scan", SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["privacy_scan"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_scanner_detects_denied_word_without_matching_inside_coverage() -> None:
    scanner = load_scanner()
    denied = base64.b64decode("dmVyYQ==").decode()
    findings = scanner.scan_content("sample.md", f"name {denied} here\ncoverage is fine\n")
    assert [finding.rule for finding in findings] == [f"forbidden-term:{denied}"]


def test_scanner_detects_network_and_secret_shapes() -> None:
    scanner = load_scanner()
    private_ip = "192" + ".168.1.10"
    token = "123456789" + ":" + ("A" * 35)
    findings = scanner.scan_content("sample.md", f"host {private_ip}\ntoken {token}\n")
    rules = {finding.rule for finding in findings}
    assert "private-ip" in rules
    assert "telegram-bot-token" in rules


def test_range_scan_checks_intermediate_trees_and_commit_messages(tmp_path) -> None:
    scanner = load_scanner()
    denied = base64.b64decode("dmVyYQ==").decode()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_NAME": "Tester",
                "GIT_AUTHOR_EMAIL": "tester@example.invalid",
                "GIT_COMMITTER_NAME": "Tester",
                "GIT_COMMITTER_EMAIL": "tester@example.invalid",
            },
        )

    git("init")
    (tmp_path / "sample.txt").write_text("clean\n")
    git("add", "sample.txt")
    git("commit", "-m", "clean start")
    first = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    (tmp_path / "sample.txt").write_text("still clean but message will not be\n")
    git("add", "sample.txt")
    git("commit", "-m", f"reviewed by {denied}")

    findings = scanner.scan_range(tmp_path, f"{first}..HEAD")
    assert any(finding.path.startswith("commit:") for finding in findings)


def _git_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.invalid",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.invalid",
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=_git_env()
    )
    return result.stdout.strip()


def _init_clean_repo(repo: Path) -> str:
    _git(repo, "init")
    (repo / "sample.txt").write_text("clean\n")
    _git(repo, "add", "sample.txt")
    _git(repo, "commit", "-m", "clean start")
    return _git(repo, "rev-parse", "HEAD")


def _install_scanner(repo: Path) -> None:
    probe_dir = repo / "probes"
    probe_dir.mkdir(exist_ok=True)
    shutil.copy(ROOT / "probes" / "privacy_scan.py", probe_dir / "privacy_scan.py")


def test_scan_ref_flags_branch_and_tag_names() -> None:
    scanner = load_scanner()
    denied = base64.b64decode("dmVyYQ==").decode()
    branch = scanner.scan_ref_name(f"refs/heads/{denied}")
    tag = scanner.scan_ref_name(f"refs/tags/{denied}-release")
    clean = scanner.scan_ref_name("refs/heads/feature-safe")
    assert branch and branch[0].path.startswith("ref:refs/heads/")
    assert tag and tag[0].path.startswith("ref:refs/tags/")
    assert clean == []


def test_scan_tag_object_flags_annotated_message_and_ignores_lightweight(tmp_path) -> None:
    scanner = load_scanner()
    denied = base64.b64decode("dmVyYQ==").decode()
    _init_clean_repo(tmp_path)
    _git(tmp_path, "tag", "-a", "annotated", "-m", f"reviewed by {denied}")
    _git(tmp_path, "tag", "lightweight")
    annotated = _git(tmp_path, "rev-parse", "annotated")
    lightweight = _git(tmp_path, "rev-parse", "lightweight")

    findings = scanner.scan_tag_object(tmp_path, annotated)
    assert findings and findings[0].path.startswith("tag:")
    assert scanner.scan_tag_object(tmp_path, lightweight) == []


def test_content_file_mode_uses_supplied_label(tmp_path) -> None:
    denied = base64.b64decode("dmVyYQ==").decode()
    metadata = tmp_path / "metadata.txt"
    metadata.write_text(f"reviewed by {denied}\n")
    result = subprocess.run(
        [
            sys.executable,
            str(SCANNER_PATH),
            "--content-file",
            str(metadata),
            "--label",
            "pr-body",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "pr-body" in result.stderr


def test_github_event_mode_scans_pr_metadata_and_refs(tmp_path) -> None:
    denied = base64.b64decode("dmVyYQ==").decode()
    payload = {
        "ref": "refs/heads/main",
        "pull_request": {
            "title": "clean title",
            "body": f"reviewed by {denied}",
            "head": {"ref": "feature-safe"},
            "base": {"ref": "main"},
        },
    }
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    result = subprocess.run(
        [sys.executable, str(SCANNER_PATH), "--github-event", str(event)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "github-event:pull-request:body" in result.stderr


def test_pre_push_hook_flags_denied_refname(tmp_path) -> None:
    denied = base64.b64decode("dmVyYQ==").decode()
    sha = _init_clean_repo(tmp_path)
    _install_scanner(tmp_path)
    zero = "0" * 40
    line = f"refs/heads/{denied} {sha} refs/heads/{denied} {zero}\n"
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "probes" / "privacy-pre-push")],
        cwd=tmp_path,
        input=line,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "forbidden-term" in result.stderr


def test_pre_push_hook_allows_delete_of_denied_refname(tmp_path) -> None:
    denied = base64.b64decode("dmVyYQ==").decode()
    sha = _init_clean_repo(tmp_path)
    _install_scanner(tmp_path)
    zero = "0" * 40
    line = f"refs/heads/{denied} {zero} refs/heads/{denied} {sha}\n"
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "probes" / "privacy-pre-push")],
        cwd=tmp_path,
        input=line,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pre_push_hook_flags_annotated_tag_message(tmp_path) -> None:
    denied = base64.b64decode("dmVyYQ==").decode()
    _init_clean_repo(tmp_path)
    _install_scanner(tmp_path)
    _git(tmp_path, "tag", "-a", "clean-tag", "-m", f"reviewed by {denied}")
    tag_sha = _git(tmp_path, "rev-parse", "clean-tag")
    zero = "0" * 40
    line = f"refs/tags/clean-tag {tag_sha} refs/tags/clean-tag {zero}\n"
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "probes" / "privacy-pre-push")],
        cwd=tmp_path,
        input=line,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert ":message" in result.stderr


def test_ci_trigger_and_privacy_step_cover_tag_pushes() -> None:
    """Tag pushes must reach CI and scan the tag object, not just the peeled commit."""
    import yaml

    data = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    triggers = data.get(True, data.get("on", {}))
    assert triggers["push"]["tags"] == ["**"]

    steps = data["jobs"]["test"]["steps"]
    privacy = next(step for step in steps if step.get("name") == "Privacy scan")
    script = privacy["run"]
    assert "github.ref_type" in script
    assert '--scan-tag-object "$tag_object"' in script
    assert "^{commit}" in script
    assert "--github-event" in script


def test_tracked_tree_is_clean() -> None:
    result = subprocess.run(
        [sys.executable, str(SCANNER_PATH), "--worktree"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
