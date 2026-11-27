#!/usr/bin/env python3
"""Fail-fast scan for private operational material in publishable content.

The substring denylist is base64-encoded so the scanner does not itself trip the
scan. This is not secrecy; it only keeps the policy file textually clean.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 2_000_000

# Keep substring entries ordered longest-first where one term contains another.
# Word entries use non-alphanumeric boundaries so ordinary words such as
# "coverage" do not trip a persona-name rule.
_FORBIDDEN_SUBSTRINGS_B64 = (
    "QGVtaXJpc3dpZmVib3Q=",
    "Y2xpdG9yaXM=",
    "YWZ0ZXJjYXJl",
    "Nzk0NTkwNTM2MQ==",
    "ZGlwbG9pZC5mbGVldA==",
    "L3ZvbHVtZXMv",
    "aHVzYmFuZA==",
    "Y2Vydml4",
    "b3JnYXNt",
    "dnVsdmE=",
    "d2l2ZXM=",
    "d2l2ZQ==",
    "Y2xpdA==",
    "Y29jaw==",
    "bXdhaA==",
    "d2lmZQ==",
    "L3VzZXJzLw==",
    "L25hcy8=",
)
_FORBIDDEN_WORDS_B64 = (
    "YXVyZWxpYQ==",
    "YmVsb3ZlZA==",
    "ZGFybGluZw==",
    "dmVzcGVy",
    "c2lzdGVy",
    "aXJpcw==",
    "bG92ZQ==",
    "dmVyYQ==",
)


def _decode_terms(encoded_terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(base64.b64decode(encoded).decode("utf-8") for encoded in encoded_terms)


FORBIDDEN_SUBSTRINGS = _decode_terms(_FORBIDDEN_SUBSTRINGS_B64)
FORBIDDEN_WORDS = _decode_terms(_FORBIDDEN_WORDS_B64)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("telegram-bot-token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE)),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
)

PRIVATE_IP = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
LINUX_HOME_PATH = re.compile(r"/home/(?!USER(?:\b|/))[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    snippet: str


def _line_at(text: str, index: int) -> tuple[int, str]:
    line_no = text.count("\n", 0, index) + 1
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end == -1:
        end = len(text)
    return line_no, text[start:end]


def _redact(line: str, token: str) -> str:
    rendered = re.sub(re.escape(token), "<redacted>", line.strip(), flags=re.IGNORECASE)
    return rendered[:160]


def scan_content(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    lowered = text.lower()

    for term in FORBIDDEN_SUBSTRINGS:
        start = 0
        while True:
            index = lowered.find(term, start)
            if index == -1:
                break
            line_no, line = _line_at(text, index)
            original = line.strip()
            term_in_line = term if term in line.lower() else text[index : index + len(term)]
            findings.append(
                Finding(path, line_no, f"forbidden-term:{term}", _redact(original, term_in_line))
            )
            start = index + len(term)

    for term in FORBIDDEN_WORDS:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)
        for match in pattern.finditer(text):
            line_no, line = _line_at(text, match.start())
            findings.append(
                Finding(path, line_no, f"forbidden-term:{term}", _redact(line, match.group(0)))
            )

    for rule, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            line_no, line = _line_at(text, match.start())
            findings.append(Finding(path, line_no, rule, _redact(line, match.group(0))))

    for rule, pattern in (("private-ip", PRIVATE_IP), ("linux-home-path", LINUX_HOME_PATH)):
        for match in pattern.finditer(text):
            line_no, line = _line_at(text, match.start())
            findings.append(Finding(path, line_no, rule, _redact(line, match.group(0))))

    return findings


def scan_blob(path: str, data: bytes) -> list[Finding]:
    findings = scan_content(path, f"path:{path}\n")
    if len(data) > MAX_FILE_BYTES:
        return findings
    findings.extend(scan_content(path, data.decode("utf-8", errors="ignore")))
    return findings


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


def _decode_paths(output: bytes) -> list[str]:
    return [part.decode("utf-8", errors="surrogateescape") for part in output.split(b"\0") if part]


def iter_worktree_blobs(repo: Path):
    for path in _decode_paths(_git(repo, "ls-files", "-z")):
        file_path = repo / path
        try:
            data = file_path.read_bytes()
        except OSError:
            data = _git(repo, "show", f"HEAD:{path}")
        yield path, data


def iter_tree_blobs(repo: Path, treeish: str):
    paths = _decode_paths(_git(repo, "ls-tree", "-r", "-z", "--name-only", treeish))
    for path in paths:
        yield path, _git(repo, "show", f"{treeish}:{path}")


def scan_blobs(blobs, prefix: str = "") -> list[Finding]:
    findings: list[Finding] = []
    for path, data in blobs:
        for finding in scan_blob(path, data):
            if prefix:
                finding = Finding(
                    f"{prefix}:{finding.path}", finding.line, finding.rule, finding.snippet
                )
            findings.append(finding)
    return findings


def scan_worktree(repo: Path) -> list[Finding]:
    return scan_blobs(iter_worktree_blobs(repo))


def scan_tree(repo: Path, treeish: str) -> list[Finding]:
    return scan_blobs(iter_tree_blobs(repo, treeish), prefix=treeish[:12])


def scan_ref_name(ref: str) -> list[Finding]:
    """Scan a branch/tag ref name as publishable metadata."""
    return scan_content(f"ref:{ref}", f"{ref}\n")


def scan_tag_object(repo: Path, objectish: str) -> list[Finding]:
    """Scan an annotated tag object's message; lightweight tags are clean here."""
    object_type = _git(repo, "cat-file", "-t", objectish).decode().strip()
    if object_type != "tag":
        return []
    raw = _git(repo, "cat-file", "tag", objectish)
    message = raw.split(b"\n\n", 1)[1] if b"\n\n" in raw else b""
    return scan_content(
        f"tag:{objectish[:12]}:message", message.decode("utf-8", errors="ignore")
    )


def scan_content_file(path: Path, label: str) -> list[Finding]:
    return scan_content(label, path.read_bytes().decode("utf-8", errors="ignore"))


def scan_github_event_file(path: Path) -> list[Finding]:
    """Scan publishable metadata carried in a GitHub Actions event payload."""
    payload = json.loads(path.read_bytes().decode("utf-8", errors="ignore") or "{}")
    findings: list[Finding] = []

    def add(label: str, value) -> None:
        if isinstance(value, str) and value:
            findings.extend(scan_content(label, value))

    add("github-event:ref", payload.get("ref"))
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        add("github-event:pull-request:title", pr.get("title"))
        add("github-event:pull-request:body", pr.get("body"))
        head = pr.get("head")
        base = pr.get("base")
        if isinstance(head, dict):
            add("github-event:pull-request:head-ref", head.get("ref"))
        if isinstance(base, dict):
            add("github-event:pull-request:base-ref", base.get("ref"))
    return findings


def scan_range(repo: Path, commit_range: str) -> list[Finding]:
    commits = _git(repo, "rev-list", "--reverse", commit_range).decode().splitlines()
    findings: list[Finding] = []
    for commit in commits:
        findings.extend(scan_tree(repo, commit))
        message = _git(repo, "show", "-s", "--format=%s%n%b", commit).decode(
            "utf-8", errors="ignore"
        )
        findings.extend(scan_content(f"commit:{commit[:12]}:message", message))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--worktree", action="store_true", help="scan tracked files in the worktree")
    mode.add_argument("--tree", metavar="TREE", help="scan one git tree/commit exactly")
    mode.add_argument("--range", metavar="RANGE", help="scan each commit tree in a git range")
    mode.add_argument("--scan-ref", metavar="REF", help="scan one branch/tag ref name")
    mode.add_argument(
        "--scan-tag-object", metavar="OBJECT", help="scan an annotated tag object's message"
    )
    mode.add_argument("--content-file", metavar="PATH", help="scan arbitrary metadata text")
    mode.add_argument("--github-event", metavar="PATH", help="scan a GitHub Actions event payload")
    parser.add_argument("--repo", default=".", help="repository root (default: current directory)")
    parser.add_argument("--label", default="metadata", help="label for --content-file findings")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if args.tree:
        findings = scan_tree(repo, args.tree)
    elif args.range:
        findings = scan_range(repo, args.range)
    elif args.scan_ref is not None:
        findings = scan_ref_name(args.scan_ref)
    elif args.scan_tag_object is not None:
        findings = scan_tag_object(repo, args.scan_tag_object)
    elif args.content_file is not None:
        findings = scan_content_file(Path(args.content_file), args.label)
    elif args.github_event is not None:
        findings = scan_github_event_file(Path(args.github_event))
    else:
        findings = scan_worktree(repo)

    if findings:
        print(f"privacy scan: {len(findings)} finding(s)", file=sys.stderr)
        for finding in findings:
            print(
                f"{finding.path}:{finding.line}: {finding.rule}: {finding.snippet}",
                file=sys.stderr,
            )
        return 1

    print("privacy scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
