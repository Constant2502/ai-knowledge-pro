#!/usr/bin/env python3
"""Validate article JSON files after Codex edit tools run."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ARTICLE_PATTERN = re.compile(
    r"(^|[/\\])knowledge[/\\]articles[/\\][^/\\]+\.json$"
)
PATCH_FILE_PATTERN = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (?P<path>.+)$",
    re.MULTILINE,
)


def main() -> int:
    """Run post-tool JSON validation for changed article files."""
    payload = read_payload()
    if not payload:
        return 0

    repo_root = find_repo_root(payload)
    changed_files = extract_changed_article_files(payload)
    if not changed_files:
        return 0

    existing_files = [
        path for path in changed_files if (repo_root / path).is_file()
    ]
    if not existing_files:
        return 0

    validation_output = run_validator(repo_root, existing_files)
    if validation_output.returncode == 0:
        return 0

    sys.stderr.write(
        "Knowledge article JSON validation failed after edit.\n\n"
    )
    sys.stderr.write(validation_output.stdout)
    sys.stderr.write(validation_output.stderr)
    return 2


def read_payload() -> dict[str, Any] | None:
    """Read and parse the Codex hook payload from stdin."""
    try:
        raw_payload = sys.stdin.read()
    except OSError:
        return None

    if not raw_payload.strip():
        return None

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    return payload


def find_repo_root(payload: dict[str, Any]) -> Path:
    """Resolve the repository root for validator execution."""
    cwd = payload.get("cwd")
    start_dir = Path(cwd) if isinstance(cwd, str) else Path.cwd()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return start_dir

    return Path(result.stdout.strip())


def extract_changed_article_files(payload: dict[str, Any]) -> list[Path]:
    """Extract article JSON paths touched by the completed tool call."""
    tool_input = payload.get("tool_input")
    candidates: list[str] = []

    if isinstance(tool_input, dict):
        candidates.extend(extract_explicit_paths(tool_input))
        command = tool_input.get("command")
        if isinstance(command, str):
            candidates.extend(extract_patch_paths(command))

    return deduplicate_article_paths(candidates)


def extract_explicit_paths(tool_input: dict[str, Any]) -> list[str]:
    """Extract direct path fields from hook tool input."""
    candidates: list[str] = []
    for key in ("file_path", "filePath", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            candidates.append(value)
    return candidates


def extract_patch_paths(command: str) -> list[str]:
    """Extract file paths from an apply_patch command payload."""
    return [
        match.group("path").strip()
        for match in PATCH_FILE_PATTERN.finditer(command)
    ]


def deduplicate_article_paths(paths: list[str]) -> list[Path]:
    """Keep unique knowledge article JSON paths in input order."""
    article_paths: list[Path] = []
    seen: set[str] = set()

    for raw_path in paths:
        normalized_path = raw_path.strip()
        if not ARTICLE_PATTERN.search(normalized_path):
            continue

        if normalized_path in seen:
            continue

        seen.add(normalized_path)
        article_paths.append(Path(normalized_path))

    return article_paths


def run_validator(
    repo_root: Path,
    article_paths: list[Path],
) -> subprocess.CompletedProcess[str]:
    """Run the repository JSON validator for changed article files."""
    command = [
        "python3",
        str(repo_root / "hooks" / "validate_json.py"),
        *[str(repo_root / path) for path in article_paths],
    ]
    try:
        return subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr=f"failed to run validator: {exc}\n",
        )


if __name__ == "__main__":
    raise SystemExit(main())
