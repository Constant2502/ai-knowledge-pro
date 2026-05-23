#!/usr/bin/env python3
"""Validate knowledge article JSON files."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from glob import glob
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse


REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "summary": str,
    "tags": list,
    "status": str,
}

SEQUENCE_ID_PATTERN = re.compile(
    r"^(?P<source>[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*)-"
    r"(?P<date>\d{8})-(?P<sequence>\d{3})$"
)
SLUG_ID_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-"
    r"(?P<source>[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*)-"
    r"(?P<slug>[a-z0-9][a-z0-9_-]*)$"
)
ALLOWED_STATUSES = {"draft", "review", "published", "archived"}
ALLOWED_AUDIENCES = {"beginner", "intermediate", "advanced"}
MIN_SUMMARY_CHARS = 20
GLOB_CHARS = frozenset("*?[")


@dataclass
class ValidationResult:
    """Validation result for a single file."""

    path: Path
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Return whether this file passed validation."""
        return not self.errors


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate knowledge article JSON files.",
        usage="python hooks/validate_json.py <json_file> [json_file2 ...]",
    )
    parser.add_argument(
        "json_files",
        nargs="+",
        help="JSON file paths or glob patterns, for example '*.json'.",
    )
    return parser.parse_args(argv)


def expand_inputs(inputs: Sequence[str]) -> tuple[list[Path], list[str]]:
    """Expand file inputs and glob patterns into unique paths."""
    paths: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()

    for raw_input in inputs:
        expanded_paths = expand_one_input(raw_input)
        if not expanded_paths:
            errors.append(f"{raw_input}: no files matched")
            continue

        for path in expanded_paths:
            normalized_path = path.resolve()
            if normalized_path in seen:
                continue
            seen.add(normalized_path)
            paths.append(path)

    return paths, errors


def expand_one_input(raw_input: str) -> list[Path]:
    """Expand one path or glob pattern."""
    if has_glob(raw_input):
        return sorted(Path(match) for match in glob(raw_input, recursive=True))
    return [Path(raw_input)]


def has_glob(raw_input: str) -> bool:
    """Return whether the input contains glob metacharacters."""
    return any(char in raw_input for char in GLOB_CHARS)


def validate_file(path: Path) -> ValidationResult:
    """Validate one JSON file."""
    result = ValidationResult(path=path)

    if not path.exists():
        result.errors.append("file does not exist")
        return result

    if not path.is_file():
        result.errors.append("path is not a file")
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        result.errors.append(f"file is not valid UTF-8: {exc}")
        return result
    except json.JSONDecodeError as exc:
        result.errors.append(f"invalid JSON: {exc}")
        return result

    if not isinstance(data, dict):
        result.errors.append("top-level JSON value must be an object")
        return result

    validate_required_fields(data, result)
    validate_id(data, result)
    validate_status(data, result)
    validate_url(data, result)
    validate_summary(data, result)
    validate_tags(data, result)
    validate_optional_score(data, result)
    validate_optional_audience(data, result)

    return result


def validate_required_fields(
    data: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Validate required field presence and type."""
    for field_name, expected_type in REQUIRED_FIELDS.items():
        if field_name not in data:
            result.errors.append(f"missing required field '{field_name}'")
            continue

        if not isinstance(data[field_name], expected_type):
            actual_type = type(data[field_name]).__name__
            expected_name = expected_type.__name__
            result.errors.append(
                f"field '{field_name}' must be {expected_name}, "
                f"got {actual_type}"
            )


def validate_id(data: dict[str, Any], result: ValidationResult) -> None:
    """Validate article ID format."""
    article_id = data.get("id")
    if not isinstance(article_id, str):
        return

    sequence_match = SEQUENCE_ID_PATTERN.fullmatch(article_id)
    if sequence_match:
        try:
            datetime.strptime(sequence_match.group("date"), "%Y%m%d")
        except ValueError:
            result.errors.append(
                "field 'id' contains an invalid YYYYMMDD date"
            )
        return

    slug_match = SLUG_ID_PATTERN.fullmatch(article_id)
    if slug_match:
        try:
            datetime.strptime(slug_match.group("date"), "%Y-%m-%d")
        except ValueError:
            result.errors.append(
                "field 'id' contains an invalid YYYY-MM-DD date"
            )
        return

    if not sequence_match and not slug_match:
        result.errors.append(
            "field 'id' must match {source}-{YYYYMMDD}-{NNN} "
            "or {YYYY-MM-DD}-{source}-{slug}, for example "
            "github-20260317-001 or 2026-05-23-github-trending-owner-repo"
        )


def validate_status(data: dict[str, Any], result: ValidationResult) -> None:
    """Validate article status."""
    status = data.get("status")
    if not isinstance(status, str):
        return

    if status not in ALLOWED_STATUSES:
        allowed = "/".join(sorted(ALLOWED_STATUSES))
        result.errors.append(f"field 'status' must be one of {allowed}")


def validate_url(data: dict[str, Any], result: ValidationResult) -> None:
    """Validate source URL format."""
    source_url = data.get("source_url")
    if not isinstance(source_url, str):
        return

    parsed_url = urlparse(source_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or any(char.isspace() for char in source_url)
    ):
        result.errors.append("field 'source_url' must be a valid http(s) URL")


def validate_summary(data: dict[str, Any], result: ValidationResult) -> None:
    """Validate summary length."""
    summary = data.get("summary")
    if not isinstance(summary, str):
        return

    if count_non_whitespace_chars(summary) < MIN_SUMMARY_CHARS:
        result.errors.append(
            f"field 'summary' must contain at least "
            f"{MIN_SUMMARY_CHARS} non-whitespace characters"
        )


def count_non_whitespace_chars(value: str) -> int:
    """Count non-whitespace characters in a string."""
    return sum(1 for char in value if not char.isspace())


def validate_tags(data: dict[str, Any], result: ValidationResult) -> None:
    """Validate tags list."""
    tags = data.get("tags")
    if not isinstance(tags, list):
        return

    if not tags:
        result.errors.append("field 'tags' must contain at least 1 item")


def validate_optional_score(
    data: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Validate optional score field."""
    if "score" not in data:
        return

    score = data["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        result.errors.append("field 'score' must be a number from 1 to 10")
        return

    if score < 1 or score > 10:
        result.errors.append("field 'score' must be between 1 and 10")


def validate_optional_audience(
    data: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Validate optional audience field."""
    if "audience" not in data:
        return

    audience = data["audience"]
    if not isinstance(audience, str) or audience not in ALLOWED_AUDIENCES:
        allowed = "/".join(sorted(ALLOWED_AUDIENCES))
        result.errors.append(f"field 'audience' must be one of {allowed}")


def write_report(results: Sequence[ValidationResult], errors: list[str]) -> int:
    """Write validation report and return the process exit code."""
    file_error_count = sum(len(result.errors) for result in results)
    total_error_count = len(errors) + file_error_count
    valid_count = sum(1 for result in results if result.is_valid)
    invalid_count = len(results) - valid_count

    if total_error_count:
        sys.stderr.write("JSON validation failed.\n\n")
        for error in errors:
            sys.stderr.write(f"- {error}\n")
        for result in results:
            for error in result.errors:
                sys.stderr.write(f"- {result.path}: {error}\n")
        sys.stderr.write("\n")
        write_summary(
            checked_count=len(results),
            valid_count=valid_count,
            invalid_count=invalid_count,
            error_count=total_error_count,
            stream=sys.stderr,
        )
        return 1

    sys.stdout.write("JSON validation passed.\n\n")
    write_summary(
        checked_count=len(results),
        valid_count=valid_count,
        invalid_count=invalid_count,
        error_count=0,
        stream=sys.stdout,
    )
    return 0


def write_summary(
    checked_count: int,
    valid_count: int,
    invalid_count: int,
    error_count: int,
    stream: Any,
) -> None:
    """Write summary statistics."""
    stream.write("Summary:\n")
    stream.write(f"  files checked: {checked_count}\n")
    stream.write(f"  valid files:   {valid_count}\n")
    stream.write(f"  invalid files: {invalid_count}\n")
    stream.write(f"  errors:        {error_count}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the JSON validator."""
    args = parse_args(argv)
    paths, input_errors = expand_inputs(args.json_files)
    results = [validate_file(path) for path in paths]
    return write_report(results, input_errors)


if __name__ == "__main__":
    raise SystemExit(main())
