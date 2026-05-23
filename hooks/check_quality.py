#!/usr/bin/env python3
"""Score knowledge article JSON quality."""

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


SUMMARY_MAX_SCORE = 25.0
TECHNICAL_DEPTH_MAX_SCORE = 25.0
FORMAT_MAX_SCORE = 20.0
TAG_MAX_SCORE = 15.0
HOLLOW_WORD_MAX_SCORE = 15.0
TOTAL_MAX_SCORE = 100.0

ALLOWED_STATUSES = {"draft", "review", "published", "archived"}
TIMESTAMP_FIELDS = (
    "collected_at",
    "published_at",
    "created_at",
    "updated_at",
    "analyzed_at",
)
GLOB_CHARS = frozenset("*?[")

SEQUENCE_ID_PATTERN = re.compile(
    r"^(?P<source>[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*)-"
    r"(?P<date>\d{8})-(?P<sequence>\d{3})$"
)
SLUG_ID_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-"
    r"(?P<source>[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*)-"
    r"(?P<slug>[a-z0-9][a-z0-9_-]*)$"
)

TECHNICAL_KEYWORDS = {
    "ai",
    "agent",
    "llm",
    "rag",
    "mcp",
    "inference",
    "embedding",
    "vector",
    "model",
    "eval",
    "fine-tuning",
    "coding agent",
    "semantic search",
    "knowledge graph",
    "multimodal",
    "tts",
    "模型",
    "推理",
    "向量",
    "智能体",
    "语义搜索",
    "知识图谱",
    "多模态",
}

STANDARD_TAGS = {
    "academic writing",
    "agent",
    "agent memory",
    "agent skills",
    "agentic video",
    "ai",
    "ai coding",
    "ai engineering",
    "ai infrastructure",
    "application architecture",
    "captioning",
    "claude code",
    "code understanding",
    "coding agent",
    "developer learning",
    "developer tool",
    "education",
    "eval",
    "inference",
    "knowledge graph",
    "learning resource",
    "llm",
    "local first",
    "mcp",
    "memory",
    "model serving",
    "multimodal",
    "multimodal ai",
    "notebook",
    "persistence",
    "personal ai",
    "prompt engineering",
    "rag",
    "reliability",
    "research",
    "research agent",
    "semantic search",
    "tts",
    "vector search",
    "vibe coding",
    "video understanding",
    "voice ai",
    "workflow",
}

CHINESE_HOLLOW_WORDS = {
    "赋能",
    "抓手",
    "闭环",
    "打通",
    "全链路",
    "底层逻辑",
    "颗粒度",
    "对齐",
    "拉通",
    "沉淀",
    "强大的",
    "革命性的",
}
ENGLISH_HOLLOW_WORDS = {
    "best-in-class",
    "breakthrough",
    "cutting-edge",
    "game-changing",
    "groundbreaking",
    "next-generation",
    "paradigm-shifting",
    "revolutionary",
    "robust",
    "seamless",
    "state-of-the-art",
    "world-class",
}


@dataclass
class DimensionScore:
    """Score for one quality dimension."""

    name: str
    score: float
    max_score: float
    detail: str


@dataclass
class QualityReport:
    """Quality report for one article file."""

    path: Path
    total_score: float
    grade: str
    dimensions: list[DimensionScore] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Score knowledge article JSON quality.",
        usage="python hooks/check_quality.py <json_file> [json_file2 ...]",
    )
    parser.add_argument(
        "json_files",
        nargs="+",
        help="JSON file paths or glob patterns, for example '*.json'.",
    )
    return parser.parse_args(argv)


def expand_inputs(inputs: Sequence[str]) -> tuple[list[Path], list[str]]:
    """Expand paths and glob patterns into unique paths."""
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
    """Expand one input path or glob pattern."""
    if has_glob(raw_input):
        return sorted(Path(match) for match in glob(raw_input, recursive=True))
    return [Path(raw_input)]


def has_glob(raw_input: str) -> bool:
    """Return whether the input contains glob metacharacters."""
    return any(char in raw_input for char in GLOB_CHARS)


def build_error_report(path: Path, error: str) -> QualityReport:
    """Build a zero-score report for unreadable or invalid files."""
    return QualityReport(
        path=path,
        total_score=0.0,
        grade="C",
        errors=[error],
    )


def score_file(path: Path) -> QualityReport:
    """Score one JSON file."""
    if not path.exists():
        return build_error_report(path, "file does not exist")

    if not path.is_file():
        return build_error_report(path, "path is not a file")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        return build_error_report(path, f"file is not valid UTF-8: {exc}")
    except json.JSONDecodeError as exc:
        return build_error_report(path, f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        return build_error_report(path, "top-level JSON value must be object")

    dimensions = [
        score_summary(data),
        score_technical_depth(data),
        score_format(data),
        score_tags(data),
        score_hollow_words(data),
    ]
    total_score = sum(dimension.score for dimension in dimensions)

    return QualityReport(
        path=path,
        total_score=round(total_score, 2),
        grade=grade_for(total_score),
        dimensions=dimensions,
    )


def score_summary(data: dict[str, Any]) -> DimensionScore:
    """Score summary quality."""
    summary = data.get("summary")
    if not isinstance(summary, str):
        return DimensionScore(
            name="摘要质量",
            score=0.0,
            max_score=SUMMARY_MAX_SCORE,
            detail="summary missing or not a string",
        )

    length = count_non_whitespace_chars(summary)
    keyword_count = count_technical_keywords(summary)
    keyword_bonus = min(keyword_count * 2.0, 5.0)

    if length >= 50:
        score = SUMMARY_MAX_SCORE
        detail = f"{length} chars, full length score"
    elif length >= 20:
        length_score = 15.0 + ((length - 20) / 30.0) * 5.0
        score = min(SUMMARY_MAX_SCORE, length_score + keyword_bonus)
        detail = (
            f"{length} chars, basic length score, "
            f"{keyword_count} technical keywords"
        )
    else:
        length_score = (length / 20.0) * 10.0
        score = min(15.0, length_score + keyword_bonus)
        detail = (
            f"{length} chars, below 20-char baseline, "
            f"{keyword_count} technical keywords"
        )

    return DimensionScore(
        name="摘要质量",
        score=round(score, 2),
        max_score=SUMMARY_MAX_SCORE,
        detail=detail,
    )


def score_technical_depth(data: dict[str, Any]) -> DimensionScore:
    """Score technical depth from article score field."""
    raw_score = data.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        return DimensionScore(
            name="技术深度",
            score=0.0,
            max_score=TECHNICAL_DEPTH_MAX_SCORE,
            detail="score missing or not numeric",
        )

    if raw_score < 1 or raw_score > 10:
        return DimensionScore(
            name="技术深度",
            score=0.0,
            max_score=TECHNICAL_DEPTH_MAX_SCORE,
            detail=f"score={raw_score} outside 1-10 range",
        )

    mapped_score = ((float(raw_score) - 1.0) / 9.0) * 25.0
    return DimensionScore(
        name="技术深度",
        score=round(mapped_score, 2),
        max_score=TECHNICAL_DEPTH_MAX_SCORE,
        detail=f"score={raw_score} mapped to 0-25",
    )


def score_format(data: dict[str, Any]) -> DimensionScore:
    """Score format compliance."""
    checks = [
        ("id", is_valid_id(data.get("id"))),
        ("title", is_non_empty_string(data.get("title"))),
        ("source_url", is_valid_url(data.get("source_url"))),
        ("status", data.get("status") in ALLOWED_STATUSES),
        ("timestamp", has_valid_timestamp(data)),
    ]
    passed = [name for name, is_valid in checks if is_valid]
    failed = [name for name, is_valid in checks if not is_valid]
    score = len(passed) * 4.0

    detail = f"passed: {', '.join(passed) or 'none'}"
    if failed:
        detail += f"; failed: {', '.join(failed)}"

    return DimensionScore(
        name="格式规范",
        score=score,
        max_score=FORMAT_MAX_SCORE,
        detail=detail,
    )


def score_tags(data: dict[str, Any]) -> DimensionScore:
    """Score tag precision."""
    tags = data.get("tags")
    if not isinstance(tags, list) or not tags:
        return DimensionScore(
            name="标签精度",
            score=0.0,
            max_score=TAG_MAX_SCORE,
            detail="tags missing, not a list, or empty",
        )

    normalized_tags = [
        normalize_tag(tag)
        for tag in tags
        if isinstance(tag, str) and tag.strip()
    ]
    if not normalized_tags:
        return DimensionScore(
            name="标签精度",
            score=0.0,
            max_score=TAG_MAX_SCORE,
            detail="tags contain no non-empty strings",
        )

    legal_count = sum(1 for tag in normalized_tags if tag in STANDARD_TAGS)
    illegal_tags = [
        tags[index]
        for index, tag in enumerate(normalized_tags)
        if tag not in STANDARD_TAGS
    ]
    if 1 <= len(normalized_tags) <= 3:
        count_score = 5.0
    elif len(normalized_tags) <= 5:
        count_score = 3.0
    else:
        count_score = 1.0

    legal_score = (legal_count / len(normalized_tags)) * 10.0
    score = count_score + legal_score

    detail = (
        f"{len(normalized_tags)} tags, {legal_count} standard tags, "
        f"count score={count_score:g}"
    )
    if illegal_tags:
        detail += f"; non-standard: {', '.join(map(str, illegal_tags))}"

    return DimensionScore(
        name="标签精度",
        score=round(min(TAG_MAX_SCORE, score), 2),
        max_score=TAG_MAX_SCORE,
        detail=detail,
    )


def score_hollow_words(data: dict[str, Any]) -> DimensionScore:
    """Score hollow-word usage."""
    text = collect_text(data)
    matches = find_hollow_words(text)
    penalty = min(len(matches) * 3.0, HOLLOW_WORD_MAX_SCORE)
    score = HOLLOW_WORD_MAX_SCORE - penalty

    if matches:
        detail = f"found hollow words: {', '.join(matches)}"
    else:
        detail = "no hollow words found"

    return DimensionScore(
        name="空洞词检测",
        score=score,
        max_score=HOLLOW_WORD_MAX_SCORE,
        detail=detail,
    )


def count_non_whitespace_chars(value: str) -> int:
    """Count non-whitespace characters."""
    return sum(1 for char in value if not char.isspace())


def count_technical_keywords(summary: str) -> int:
    """Count technical keywords contained in summary."""
    normalized_summary = summary.casefold()
    return sum(
        1
        for keyword in TECHNICAL_KEYWORDS
        if keyword.casefold() in normalized_summary
    )


def is_non_empty_string(value: Any) -> bool:
    """Return whether a value is a non-empty string."""
    return isinstance(value, str) and bool(value.strip())


def is_valid_id(value: Any) -> bool:
    """Return whether an article ID has an accepted format."""
    if not isinstance(value, str):
        return False

    sequence_match = SEQUENCE_ID_PATTERN.fullmatch(value)
    if sequence_match:
        return is_valid_date(sequence_match.group("date"), "%Y%m%d")

    slug_match = SLUG_ID_PATTERN.fullmatch(value)
    if slug_match:
        return is_valid_date(slug_match.group("date"), "%Y-%m-%d")

    return False


def is_valid_date(value: str, date_format: str) -> bool:
    """Return whether value matches the date format."""
    try:
        datetime.strptime(value, date_format)
    except ValueError:
        return False
    return True


def is_valid_url(value: Any) -> bool:
    """Return whether value is a valid http(s) URL."""
    if not isinstance(value, str):
        return False

    parsed_url = urlparse(value)
    return (
        parsed_url.scheme in {"http", "https"}
        and bool(parsed_url.netloc)
        and not any(char.isspace() for char in value)
    )


def has_valid_timestamp(data: dict[str, Any]) -> bool:
    """Return whether an article has any valid timestamp field."""
    return any(
        is_valid_timestamp_value(data.get(field_name))
        for field_name in TIMESTAMP_FIELDS
    )


def is_valid_timestamp_value(value: Any) -> bool:
    """Return whether a value looks like a valid date or datetime."""
    if not isinstance(value, str) or not value.strip():
        return False

    normalized_value = value.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalized_value)
    except ValueError:
        return is_valid_date(value.strip(), "%Y-%m-%d")
    return True


def normalize_tag(tag: str) -> str:
    """Normalize one tag for standard-list comparison."""
    return " ".join(tag.strip().casefold().replace("_", " ").split())


def collect_text(value: Any) -> str:
    """Collect all string content from a JSON-like value."""
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        return " ".join(collect_text(item) for item in value)

    if isinstance(value, dict):
        return " ".join(collect_text(item) for item in value.values())

    return ""


def find_hollow_words(text: str) -> list[str]:
    """Find hollow words in article text."""
    matches: list[str] = []
    lower_text = text.casefold()

    for word in sorted(CHINESE_HOLLOW_WORDS):
        if word in text:
            matches.append(word)

    for word in sorted(ENGLISH_HOLLOW_WORDS):
        if word.casefold() in lower_text:
            matches.append(word)

    return matches


def grade_for(score: float) -> str:
    """Return grade label for total score."""
    if score >= 80:
        return "A"
    if score >= 60:
        return "B"
    return "C"


def progress_bar(score: float, max_score: float = TOTAL_MAX_SCORE) -> str:
    """Render an ASCII progress bar."""
    width = 20
    ratio = max(0.0, min(score / max_score, 1.0))
    filled = round(ratio * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def write_reports(
    reports: Sequence[QualityReport],
    input_errors: Sequence[str],
) -> int:
    """Write reports and return process exit code."""
    if input_errors:
        sys.stderr.write("Input errors:\n")
        for error in input_errors:
            sys.stderr.write(f"- {error}\n")
        sys.stderr.write("\n")

    for report in reports:
        write_one_report(report)

    total_count = len(reports)
    c_count = sum(1 for report in reports if report.grade == "C")
    a_count = sum(1 for report in reports if report.grade == "A")
    b_count = sum(1 for report in reports if report.grade == "B")
    error_count = len(input_errors) + sum(
        len(report.errors) for report in reports
    )

    sys.stdout.write("Summary:\n")
    sys.stdout.write(f"  files checked: {total_count}\n")
    sys.stdout.write(f"  A:             {a_count}\n")
    sys.stdout.write(f"  B:             {b_count}\n")
    sys.stdout.write(f"  C:             {c_count}\n")
    sys.stdout.write(f"  errors:        {error_count}\n")

    if input_errors or c_count:
        return 1
    return 0


def write_one_report(report: QualityReport) -> None:
    """Write one quality report."""
    sys.stdout.write(f"{report.path}\n")
    sys.stdout.write(
        f"  Grade {report.grade} "
        f"{progress_bar(report.total_score)} "
        f"{report.total_score:.2f}/100\n"
    )

    for error in report.errors:
        sys.stdout.write(f"  - error: {error}\n")

    for dimension in report.dimensions:
        sys.stdout.write(
            f"  - {dimension.name}: "
            f"{dimension.score:.2f}/{dimension.max_score:.0f} "
            f"({dimension.detail})\n"
        )

    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run quality scoring."""
    args = parse_args(argv)
    paths, input_errors = expand_inputs(args.json_files)
    reports = [score_file(path) for path in paths]
    return write_reports(reports, input_errors)


if __name__ == "__main__":
    raise SystemExit(main())
