"""Four-step automation pipeline for the local AI knowledge base."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx

try:
    from .model_client import create_provider, chat_with_retry
    from .model_client import tracker as cost_tracker
except ImportError:
    from model_client import create_provider, chat_with_retry
    from model_client import tracker as cost_tracker


LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "knowledge" / "raw"
ARTICLES_DIR = PROJECT_ROOT / "knowledge" / "articles"
LOG_DIR = PROJECT_ROOT / "knowledge" / "logs"
VALIDATOR_PATH = PROJECT_ROOT / "hooks" / "validate_json.py"

SUPPORTED_SOURCES = {"github", "rss"}
VALID_STEPS = {1, 2, 3, 4}
DEFAULT_STEPS = (1, 2, 3, 4)
DEFAULT_LIMIT = 20
REQUEST_TIMEOUT_SECONDS = 30.0
PIPELINE_SOURCE = "pipeline"
STATUS_DRAFT = "draft"
DEFAULT_DISTRIBUTION_CHANNELS = ["telegram", "feishu"]
STEP_NAMES = {
    1: "collect",
    2: "save_raw",
    3: "analyze_organize",
    4: "save_articles",
}

AI_KEYWORDS = (
    "ai",
    "artificial intelligence",
    "llm",
    "large language model",
    "agent",
    "agents",
    "rag",
    "retrieval augmented generation",
    "mcp",
    "model context protocol",
    "inference",
    "embedding",
    "vector",
    "fine-tuning",
    "eval",
    "multimodal",
    "transformer",
    "生成式",
    "大模型",
    "智能体",
    "推理",
    "向量",
    "检索增强",
)

GITHUB_SEARCH_QUERIES = (
    '"large language model" in:name,description,readme',
    "llm agent in:name,description,readme",
    "rag ai in:name,description,readme",
    '"generative ai" in:name,description,readme',
)

DEFAULT_RSS_FEEDS = (
    "https://hnrss.org/newest?q=AI",
    "https://hnrss.org/newest?q=LLM",
    "https://hnrss.org/newest?q=agent",
    "https://rss.arxiv.org/rss/cs.AI",
    "https://rss.arxiv.org/rss/cs.CL",
)

RSS_ITEM_PATTERN = re.compile(
    r"<(?:item|entry)\b[^>]*>(?P<body>.*?)</(?:item|entry)>",
    re.IGNORECASE | re.DOTALL,
)
TAG_PATTERN_TEMPLATE = r"<{tag}\b[^>]*>(?P<value>.*?)</{tag}>"
LINK_HREF_PATTERN = re.compile(
    r"<link\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class CollectedItem:
    """Content collected from an upstream source."""

    source: str
    source_type: str
    title: str
    source_url: str
    published_at: str
    collected_at: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalyzedItem:
    """Collected content plus LLM analysis."""

    collected_item: CollectedItem
    summary: str
    content: str
    tags: list[str]
    score: int
    score_reason: str
    ai_relevance_reason: str
    key_points: list[str]
    risks: list[str]


@dataclass(frozen=True)
class PipelineResult:
    """Files and counts produced by one pipeline run."""

    run_id: str
    started_at: str
    finished_at: str
    sources: list[str]
    limit: int
    steps: list[int]
    dry_run: bool
    raw_file: Path | None
    raw_input_file: Path | None
    article_files: list[Path]
    collected_count: int
    analyzed_count: int
    saved_count: int
    skipped_duplicate_count: int
    cost_report: dict[str, Any]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the AI knowledge base collection pipeline.",
    )
    parser.add_argument(
        "--sources",
        default="github,rss",
        help="Comma-separated sources to collect from: github,rss.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum number of collected items to process.",
    )
    parser.add_argument(
        "--step",
        action="append",
        dest="steps",
        help=(
            "Pipeline step to run. Can be repeated, for example "
            "--step 1 --step 2. Defaults to 1,2,3,4. "
            "Steps: 1=collect, 2=save raw, "
            "3=analyze and organize, 4=save articles."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the pipeline without writing raw or article files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed logging.",
    )
    parser.add_argument(
        "--rss-feeds",
        default=os.getenv("RSS_FEEDS", ""),
        help="Comma-separated RSS feed URLs. Defaults to built-in AI feeds.",
    )
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    """Configure process logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s:%(message)s",
    )


def parse_sources(raw_sources: str) -> list[str]:
    """Parse and validate source names."""
    sources = [
        source.strip().lower()
        for source in raw_sources.split(",")
        if source.strip()
    ]
    if not sources:
        raise ValueError("--sources must include at least one source.")

    invalid_sources = sorted(set(sources) - SUPPORTED_SOURCES)
    if invalid_sources:
        supported = ", ".join(sorted(SUPPORTED_SOURCES))
        invalid = ", ".join(invalid_sources)
        raise ValueError(
            f"Unsupported sources: {invalid}. Supported sources: {supported}."
        )

    return list(dict.fromkeys(sources))


def parse_steps(raw_steps: Sequence[str] | None) -> list[int]:
    """Parse and validate selected pipeline steps."""
    if not raw_steps:
        return list(DEFAULT_STEPS)

    steps: list[int] = []
    for raw_step in raw_steps:
        for step_part in raw_step.split(","):
            step_text = step_part.strip()
            if not step_text:
                continue
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError(
                    f"Unsupported step '{step_text}'. "
                    "Supported steps: 1, 2, 3, 4."
                ) from exc
            if step not in VALID_STEPS:
                raise ValueError(
                    f"Unsupported step '{step}'. "
                    "Supported steps: 1, 2, 3, 4."
                )
            if step not in steps:
                steps.append(step)

    if not steps:
        raise ValueError("--step must include at least one step.")

    normalized_steps = sorted(steps)
    validate_step_dependencies(normalized_steps)
    return normalized_steps


def validate_step_dependencies(steps: Sequence[int]) -> None:
    """Validate step combinations that require in-memory state."""
    if not steps:
        raise ValueError("--step must include at least one step.")
    invalid_steps = sorted(set(steps) - VALID_STEPS)
    if invalid_steps:
        invalid = ", ".join(map(str, invalid_steps))
        raise ValueError(
            f"Unsupported steps: {invalid}. Supported steps: 1, 2, 3, 4."
        )
    if 2 in steps and 1 not in steps:
        raise ValueError("--step 2 requires --step 1 in the same run.")
    if 4 in steps and 3 not in steps:
        raise ValueError("--step 4 requires --step 3 in the same run.")


def run_pipeline(
    sources: Sequence[str],
    limit: int,
    rss_feeds: Sequence[str],
    steps: Sequence[int] | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
    started_at: str | None = None,
) -> PipelineResult:
    """Run selected pipeline steps."""
    if limit < 1:
        raise ValueError("--limit must be at least 1.")

    selected_steps = sorted(set(steps or DEFAULT_STEPS))
    validate_step_dependencies(selected_steps)
    run_id = run_id or build_run_id()
    started_at = started_at or now_iso()
    collected_at = started_at
    cost_tracker.reset()
    LOGGER.info(
        "Starting pipeline run_id=%s sources=%s steps=%s",
        run_id,
        ",".join(sources),
        ",".join(map(str, selected_steps)),
    )

    collected_items: list[CollectedItem] = []
    raw_file: Path | None = None
    raw_input_file: Path | None = None
    analyzed_items: list[AnalyzedItem] = []
    article_records: list[dict[str, Any]] = []
    skipped_duplicate_count = 0
    article_files: list[Path] = []

    if 1 in selected_steps:
        collected_items = collect_items(
            sources=sources,
            limit=limit,
            rss_feeds=rss_feeds,
            collected_at=collected_at,
        )
        LOGGER.info("Collected %s candidate items", len(collected_items))

    if 2 in selected_steps:
        raw_file = save_raw_items(
            run_id=run_id,
            items=collected_items,
            dry_run=dry_run,
        )

    if 3 in selected_steps:
        if not collected_items:
            raw_input_file, collected_items = load_latest_raw_items(limit=limit)
            LOGGER.info(
                "Loaded %s candidate items from raw file: %s",
                len(collected_items),
                raw_input_file,
            )
        elif raw_file is not None:
            raw_input_file = raw_file

        provider = create_provider()
        analyzed_items = analyze_items(collected_items, provider=provider)
        LOGGER.info("Analyzed %s items", len(analyzed_items))

        article_records, skipped_duplicate_count = organize_items(analyzed_items)
        LOGGER.info(
            "Organized %s article records; skipped duplicates=%s",
            len(article_records),
            skipped_duplicate_count,
        )

    if 4 in selected_steps:
        article_files = save_articles(article_records, dry_run=dry_run)
        LOGGER.info("Saved %s article files", len(article_files))

    cost_report = cost_tracker.report()

    return PipelineResult(
        run_id=run_id,
        started_at=started_at,
        finished_at=now_iso(),
        sources=list(sources),
        limit=limit,
        steps=selected_steps,
        dry_run=dry_run,
        raw_file=raw_file,
        raw_input_file=raw_input_file,
        article_files=article_files,
        collected_count=len(collected_items),
        analyzed_count=len(analyzed_items),
        saved_count=len(article_files),
        skipped_duplicate_count=skipped_duplicate_count,
        cost_report=cost_report,
    )


def collect_items(
    sources: Sequence[str],
    limit: int,
    rss_feeds: Sequence[str],
    collected_at: str,
) -> list[CollectedItem]:
    """Collect candidate content from selected sources."""
    collected_items: list[CollectedItem] = []
    seen_urls: set[str] = set()

    for source in sources:
        if len(collected_items) >= limit:
            break

        remaining = limit - len(collected_items)
        if source == "github":
            source_items = collect_github_items(
                limit=remaining,
                collected_at=collected_at,
            )
        elif source == "rss":
            source_items = collect_rss_items(
                feed_urls=rss_feeds,
                limit=remaining,
                collected_at=collected_at,
            )
        else:
            source_items = []

        for item in source_items:
            if item.source_url in seen_urls:
                continue
            seen_urls.add(item.source_url)
            collected_items.append(item)
            if len(collected_items) >= limit:
                break

    return collected_items


def collect_github_items(limit: int, collected_at: str) -> list[CollectedItem]:
    """Collect AI-related repositories from the GitHub Search API."""
    if limit < 1:
        return []

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-knowledge-pro",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    github_token = os.getenv("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    collected_items: list[CollectedItem] = []
    seen_urls: set[str] = set()

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
        for query in GITHUB_SEARCH_QUERIES:
            if len(collected_items) >= limit:
                break

            response = client.get(
                "https://api.github.com/search/repositories",
                params={
                    "q": query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": min(50, limit),
                },
            )
            response.raise_for_status()
            payload = response.json()
            repositories = payload.get("items", [])
            if not isinstance(repositories, list):
                continue

            for repository in repositories:
                if len(collected_items) >= limit:
                    break
                item = parse_github_repository(repository, collected_at)
                if item is None or item.source_url in seen_urls:
                    continue
                seen_urls.add(item.source_url)
                collected_items.append(item)

    return collected_items


def parse_github_repository(
    repository: Any,
    collected_at: str,
) -> CollectedItem | None:
    """Convert one GitHub repository payload into a collected item."""
    if not isinstance(repository, dict):
        return None

    title = str(repository.get("full_name") or "").strip()
    source_url = str(repository.get("html_url") or "").strip()
    description = str(repository.get("description") or "").strip()
    topics = repository.get("topics") or []
    text_for_filter = " ".join(
        [title, description, " ".join(map(str, topics))]
    )

    if not title or not is_valid_url(source_url):
        return None
    if not is_ai_related(text_for_filter):
        return None

    return CollectedItem(
        source="github_search",
        source_type="repository",
        title=title,
        source_url=source_url,
        published_at=str(repository.get("created_at") or ""),
        collected_at=collected_at,
        description=description,
        metadata={
            "language": repository.get("language"),
            "stars": repository.get("stargazers_count"),
            "forks": repository.get("forks_count"),
            "updated_at": repository.get("updated_at"),
            "topics": topics if isinstance(topics, list) else [],
        },
    )


def collect_rss_items(
    feed_urls: Sequence[str],
    limit: int,
    collected_at: str,
) -> list[CollectedItem]:
    """Collect AI-related content from RSS or Atom feeds."""
    if limit < 1:
        return []

    collected_items: list[CollectedItem] = []
    seen_urls: set[str] = set()

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        for feed_url in feed_urls:
            if len(collected_items) >= limit:
                break

            response = client.get(feed_url)
            response.raise_for_status()
            for item in parse_rss_items(
                feed_url=feed_url,
                body=response.text,
                collected_at=collected_at,
            ):
                if len(collected_items) >= limit:
                    break
                if item.source_url in seen_urls:
                    continue
                seen_urls.add(item.source_url)
                collected_items.append(item)

    return collected_items


def parse_rss_items(
    feed_url: str,
    body: str,
    collected_at: str,
) -> list[CollectedItem]:
    """Parse RSS or Atom content with lightweight regular expressions."""
    items: list[CollectedItem] = []
    for match in RSS_ITEM_PATTERN.finditer(body):
        item_body = match.group("body")
        title = clean_text(extract_tag(item_body, "title"))
        link = extract_rss_link(item_body)
        description = clean_text(
            extract_tag(item_body, "description")
            or extract_tag(item_body, "summary")
            or extract_tag(item_body, "content")
        )
        published_at = clean_text(
            extract_tag(item_body, "pubDate")
            or extract_tag(item_body, "published")
            or extract_tag(item_body, "updated")
        )

        if not title or not is_valid_url(link):
            continue
        if not is_ai_related(f"{title} {description}"):
            continue

        items.append(
            CollectedItem(
                source="rss",
                source_type="article",
                title=title,
                source_url=link,
                published_at=published_at,
                collected_at=collected_at,
                description=description,
                metadata={"feed_url": feed_url},
            )
        )

    return items


def extract_tag(body: str, tag: str) -> str:
    """Extract the first tag value from an RSS item body."""
    pattern = re.compile(
        TAG_PATTERN_TEMPLATE.format(tag=re.escape(tag)),
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(body)
    if not match:
        return ""
    return match.group("value")


def extract_rss_link(body: str) -> str:
    """Extract RSS or Atom link value."""
    link = clean_text(extract_tag(body, "link"))
    if link:
        return link

    href_match = LINK_HREF_PATTERN.search(body)
    if href_match:
        return html.unescape(href_match.group("href").strip())

    return ""


def clean_text(value: str) -> str:
    """Remove simple markup and normalize whitespace."""
    unescaped = html.unescape(value or "")
    text = HTML_TAG_PATTERN.sub(" ", unescaped)
    return " ".join(text.split())


def save_raw_items(
    run_id: str,
    items: Sequence[CollectedItem],
    dry_run: bool,
) -> Path | None:
    """Save collected raw data into knowledge/raw."""
    if dry_run:
        LOGGER.info("Dry-run enabled; skipping raw file write")
        return None

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_file = RAW_DIR / f"pipeline-{run_id}.json"
    payload = {
        "run_id": run_id,
        "source": PIPELINE_SOURCE,
        "collected_at": now_iso(),
        "items": [asdict(item) for item in items],
    }
    raw_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return raw_file


def load_latest_raw_items(limit: int) -> tuple[Path, list[CollectedItem]]:
    """Load collected items from the latest pipeline raw JSON file."""
    raw_file = find_latest_pipeline_raw_file()
    payload = json.loads(raw_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Raw file must contain a JSON object: {raw_file}")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError(f"Raw file missing items list: {raw_file}")

    collected_items: list[CollectedItem] = []
    for raw_item in raw_items:
        item = parse_collected_item_payload(raw_item)
        if item is None:
            LOGGER.warning("Skipping invalid raw item in %s", raw_file)
            continue
        collected_items.append(item)
        if len(collected_items) >= limit:
            break

    if not collected_items:
        raise ValueError(f"Raw file contains no valid items: {raw_file}")

    return raw_file, collected_items


def find_latest_pipeline_raw_file() -> Path:
    """Return the latest raw JSON file produced by this pipeline."""
    raw_files = sorted(RAW_DIR.glob("pipeline-*.json"))
    if not raw_files:
        raise ValueError(
            "No pipeline raw files found. Run --step 1 --step 2 first."
        )
    return raw_files[-1]


def parse_collected_item_payload(payload: Any) -> CollectedItem | None:
    """Parse one raw JSON item into a CollectedItem."""
    if not isinstance(payload, dict):
        return None

    try:
        metadata = payload.get("metadata")
        return CollectedItem(
            source=get_required_raw_string(payload, "source"),
            source_type=get_required_raw_string(payload, "source_type"),
            title=get_required_raw_string(payload, "title"),
            source_url=get_required_raw_string(payload, "source_url"),
            published_at=get_string(payload, "published_at"),
            collected_at=get_required_raw_string(payload, "collected_at"),
            description=get_string(payload, "description"),
            metadata=metadata if isinstance(metadata, dict) else {},
        )
    except ValueError as exc:
        LOGGER.warning("Invalid raw item: %s", exc)
        return None


def get_required_raw_string(payload: dict[str, Any], key: str) -> str:
    """Return a required non-empty string from a raw item payload."""
    value = get_string(payload, key)
    if not value:
        raise ValueError(f"Raw item missing required string field: {key}")
    return value


def analyze_items(
    items: Sequence[CollectedItem],
    provider: Any,
) -> list[AnalyzedItem]:
    """Analyze collected items with an LLM."""
    analyzed_items: list[AnalyzedItem] = []
    for index, item in enumerate(items, start=1):
        LOGGER.info("Analyzing item %s/%s: %s", index, len(items), item.title)
        response = chat_with_retry(
            messages=build_analysis_messages(item),
            provider=provider,
            temperature=0.2,
        )
        analysis = parse_analysis_response(response.content, item)
        analyzed_items.append(analysis)

    return analyzed_items


def build_analysis_messages(item: CollectedItem) -> list[dict[str, str]]:
    """Build LLM messages for one collected item."""
    system_prompt = (
        "You analyze AI, LLM, Agent, RAG, inference, and developer-tool "
        "content for a local knowledge base. Return only valid JSON."
    )
    user_prompt = {
        "title": item.title,
        "source": item.source,
        "source_type": item.source_type,
        "source_url": item.source_url,
        "published_at": item.published_at,
        "description": item.description,
        "metadata": item.metadata,
        "required_json_schema": {
            "summary": "Chinese summary, at least 20 characters",
            "content": "Chinese structured explanation",
            "tags": ["AI", "LLM", "Agent"],
            "score": "integer 1-10",
            "score_reason": "specific reason for the score",
            "ai_relevance_reason": "why this belongs in AI knowledge base",
            "key_points": ["2-4 concrete points"],
            "risks": ["1-3 risks or uncertainties"],
        },
    }
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(user_prompt, ensure_ascii=False),
        },
    ]


def parse_analysis_response(
    response_content: str,
    item: CollectedItem,
) -> AnalyzedItem:
    """Parse and normalize the LLM analysis response."""
    try:
        payload = json.loads(extract_json_object(response_content))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned invalid JSON for {item.source_url}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(f"LLM returned non-object JSON for {item.source_url}.")

    summary = ensure_min_summary(
        get_string(payload, "summary") or item.description or item.title,
        item,
    )
    content = get_string(payload, "content") or item.description or summary
    tags = normalize_tags(payload.get("tags"))
    score = clamp_int(payload.get("score"), minimum=1, maximum=10, default=5)

    return AnalyzedItem(
        collected_item=item,
        summary=summary,
        content=content,
        tags=tags,
        score=score,
        score_reason=get_string(payload, "score_reason") or "LLM analyzed.",
        ai_relevance_reason=(
            get_string(payload, "ai_relevance_reason")
            or "Collected item matched AI-related source filters."
        ),
        key_points=normalize_string_list(payload.get("key_points")),
        risks=normalize_string_list(payload.get("risks")),
    )


def extract_json_object(value: str) -> str:
    """Extract a JSON object from plain text or a Markdown code fence."""
    stripped_value = value.strip()
    if stripped_value.startswith("{") and stripped_value.endswith("}"):
        return stripped_value

    fenced_match = re.search(
        r"```(?:json)?\s*(?P<json>\{.*?\})\s*```",
        stripped_value,
        re.IGNORECASE | re.DOTALL,
    )
    if fenced_match:
        return fenced_match.group("json")

    start = stripped_value.find("{")
    end = stripped_value.rfind("}")
    if start >= 0 and end > start:
        return stripped_value[start : end + 1]

    return stripped_value


def organize_items(
    analyzed_items: Sequence[AnalyzedItem],
) -> tuple[list[dict[str, Any]], int]:
    """Deduplicate, standardize, and validate article records."""
    existing_urls = load_existing_article_urls()
    article_records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    skipped_duplicate_count = 0

    for analyzed_item in analyzed_items:
        item = analyzed_item.collected_item
        if item.source_url in existing_urls or item.source_url in seen_urls:
            skipped_duplicate_count += 1
            continue

        article = build_article_record(analyzed_item)
        validate_article_record(article)
        article_records.append(article)
        seen_urls.add(item.source_url)

    return article_records, skipped_duplicate_count


def load_existing_article_urls() -> set[str]:
    """Load source URLs already present in article JSON files."""
    urls: set[str] = set()
    for path in ARTICLES_DIR.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Skipping unreadable existing article: %s", path)
            continue

        if isinstance(payload, dict) and isinstance(payload.get("source_url"), str):
            urls.add(payload["source_url"])

    return urls


def build_article_record(analyzed_item: AnalyzedItem) -> dict[str, Any]:
    """Build the final standardized article JSON record."""
    item = analyzed_item.collected_item
    source_slug = item.source.replace("_", "-")
    slug = slugify(item.title or item.source_url)
    date = date_for_article(item)
    article_id = f"{date}-{source_slug}-{slug}"

    return {
        "id": article_id,
        "title": item.title,
        "source": item.source,
        "source_url": item.source_url,
        "source_type": item.source_type,
        "published_at": item.published_at,
        "collected_at": item.collected_at,
        "summary": analyzed_item.summary,
        "content": analyzed_item.content,
        "tags": analyzed_item.tags,
        "status": STATUS_DRAFT,
        "ai_relevance": True,
        "ai_relevance_reason": analyzed_item.ai_relevance_reason,
        "key_points": analyzed_item.key_points,
        "risks": analyzed_item.risks,
        "score": analyzed_item.score,
        "score_reason": analyzed_item.score_reason,
        "distribution_channels": DEFAULT_DISTRIBUTION_CHANNELS,
    }


def validate_article_record(article: dict[str, Any]) -> None:
    """Validate a final article record before saving."""
    required_fields = {
        "id": str,
        "title": str,
        "source_url": str,
        "summary": str,
        "tags": list,
        "status": str,
    }
    for field_name, expected_type in required_fields.items():
        value = article.get(field_name)
        if not isinstance(value, expected_type):
            raise ValueError(
                f"Article field {field_name} must be "
                f"{expected_type.__name__}."
            )

    if not is_valid_url(article["source_url"]):
        raise ValueError(f"Invalid source_url: {article['source_url']}")
    if count_non_whitespace_chars(article["summary"]) < 20:
        raise ValueError(f"Summary is too short for {article['source_url']}")
    if not article["tags"]:
        raise ValueError(f"Tags must not be empty for {article['source_url']}")


def save_articles(
    article_records: Sequence[dict[str, Any]],
    dry_run: bool,
) -> list[Path]:
    """Save article records into knowledge/articles."""
    if dry_run:
        LOGGER.info("Dry-run enabled; skipping article file writes")
        return []

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []

    for article in article_records:
        path = ARTICLES_DIR / f"{article['id']}.json"
        if path.exists():
            LOGGER.warning("Skipping existing article path: %s", path)
            continue

        path.write_text(
            json.dumps(article, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        run_article_validator(path)
        saved_paths.append(path)

    return saved_paths


def run_article_validator(path: Path) -> None:
    """Run the JSON validator script for a saved article file."""
    if not VALIDATOR_PATH.exists():
        LOGGER.warning("Validator not found: %s", VALIDATOR_PATH)
        return

    result = subprocess.run(
        ["python3", str(VALIDATOR_PATH), str(path)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"Article validation failed for {path}:\n"
            f"{result.stdout}{result.stderr}"
        )


def parse_rss_feed_urls(raw_feeds: str) -> list[str]:
    """Parse RSS feed URLs from CLI or environment."""
    if raw_feeds.strip():
        feeds = [feed.strip() for feed in raw_feeds.split(",") if feed.strip()]
    else:
        feeds = list(DEFAULT_RSS_FEEDS)
    return feeds


def is_ai_related(text: str) -> bool:
    """Return whether text contains AI-related signals."""
    normalized_text = text.casefold()
    return any(keyword.casefold() in normalized_text for keyword in AI_KEYWORDS)


def is_valid_url(value: str) -> bool:
    """Return whether a value is a valid HTTP URL."""
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def slugify(value: str, max_length: int = 80) -> str:
    """Convert a title or URL into a stable lowercase slug."""
    normalized = value.casefold()
    normalized = normalized.replace("/", "-")
    slug = SLUG_PATTERN.sub("-", normalized).strip("-")
    if not slug:
        slug = "untitled"
    return slug[:max_length].strip("-")


def date_for_article(item: CollectedItem) -> str:
    """Return article date in YYYY-MM-DD format."""
    if item.collected_at:
        return item.collected_at[:10]
    return now_iso()[:10]


def build_run_id() -> str:
    """Build a timestamp-based pipeline run ID."""
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    """Return current local time in ISO 8601 format."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_string(payload: dict[str, Any], key: str) -> str:
    """Return a stripped string value from a dictionary."""
    value = payload.get(key)
    if isinstance(value, str):
        return value.strip()
    return ""


def normalize_tags(value: Any) -> list[str]:
    """Normalize LLM-provided tags."""
    if not isinstance(value, list):
        return ["AI"]

    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        if tag and tag not in tags:
            tags.append(tag)

    return tags or ["AI"]


def normalize_string_list(value: Any) -> list[str]:
    """Normalize an arbitrary value into a non-empty string list."""
    if isinstance(value, list):
        items = [
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
        if items:
            return items

    if isinstance(value, str) and value.strip():
        return [value.strip()]

    return []


def clamp_int(
    value: Any,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    """Convert a value to int and clamp it to a range."""
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        return default

    return max(minimum, min(maximum, int_value))


def ensure_min_summary(summary: str, item: CollectedItem) -> str:
    """Ensure summary is long enough for article validation."""
    if count_non_whitespace_chars(summary) >= 20:
        return summary

    fallback = f"{item.title}：{item.description or item.source_url}"
    if count_non_whitespace_chars(fallback) >= 20:
        return fallback

    return f"{fallback}，该内容因包含 AI 相关信号而进入知识库候选。"


def count_non_whitespace_chars(value: str) -> int:
    """Count non-whitespace characters."""
    return sum(1 for char in value if not char.isspace())


def log_result(result: PipelineResult, dry_run: bool) -> None:
    """Log the final pipeline result."""
    LOGGER.info(
        "Pipeline complete: steps=%s collected=%s analyzed=%s saved=%s "
        "duplicates=%s",
        ",".join(map(str, result.steps)),
        result.collected_count,
        result.analyzed_count,
        result.saved_count,
        result.skipped_duplicate_count,
    )
    LOGGER.info(
        "LLM cost summary: calls=%s total_tokens=%s estimated_cost_cny=%.8f",
        result.cost_report.get("call_count", 0),
        result.cost_report.get("total_tokens", 0),
        float(result.cost_report.get("estimated_cost_cny", 0.0)),
    )
    if result.raw_file:
        LOGGER.info("Raw file: %s", result.raw_file)
    if result.article_files:
        for path in result.article_files:
            LOGGER.info("Article file: %s", path)
    if dry_run:
        LOGGER.info("Dry-run completed; no files were written")


def write_run_log(result: PipelineResult) -> Path:
    """Write a Markdown run log for a completed pipeline execution."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"pipeline-{result.run_id}-run-log.md"
    content = build_run_log_content(result)
    log_path.write_text(content, encoding="utf-8")
    return log_path


def write_failure_run_log(
    run_id: str,
    started_at: str,
    sources: Sequence[str],
    limit: int,
    steps: Sequence[int],
    dry_run: bool,
    error: Exception,
) -> Path:
    """Write a Markdown run log for a failed pipeline execution."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"pipeline-{run_id}-run-log.md"
    raw_file = RAW_DIR / f"pipeline-{run_id}.json"
    raw_output = format_optional_path(raw_file if raw_file.exists() else None)
    sources_text = ", ".join(sources) if sources else "未解析"
    step_sequence = format_step_sequence(steps)
    finished_at = now_iso()
    content = (
        "# Agent 执行日志\n\n"
        "## 基本信息\n\n"
        f"- 运行 ID：`{run_id}`\n"
        "- 调用方：pipeline CLI\n"
        f"- 执行顺序：`{step_sequence}`\n"
        f"- 数据来源：`{sources_text}`\n"
        f"- 执行日期：`{started_at[:10]}`\n"
        f"- 开始时间：`{started_at}`\n"
        f"- 结束时间：`{finished_at}`\n"
        f"- 执行方式：`python pipeline/pipeline.py --sources "
        f"{sources_text} --limit {limit} {format_step_cli_args(steps)}`\n"
        f"- Dry-run：`{str(dry_run).lower()}`\n\n"
        "## 执行结果\n\n"
        "| 阶段 | 步骤 | 输出文件 | 状态 |\n"
        "| --- | --- | --- | --- |\n"
        f"| 1 | collect | 候选条目 | {format_failure_step_status(steps, 1)} |\n"
        f"| 2 | save_raw | {raw_output} | "
        f"{format_failure_step_status(steps, 2)} |\n"
        f"| 3 | analyze_organize | `未完成` | "
        f"{format_failure_step_status(steps, 3)} |\n"
        f"| 4 | save_articles | `未完成` | "
        f"{format_failure_step_status(steps, 4)} |\n\n"
        "## 失败信息\n\n"
        f"- 错误类型：`{type(error).__name__}`\n"
        f"- 错误信息：`{str(error)}`\n\n"
        "## 重要说明\n\n"
        "- 本次流程未完整完成，可能没有生成最终 article JSON。\n"
        "- 若外部 API 或 LLM provider 不可访问，"
        "流程会在对应阶段失败。\n"
    )
    log_path.write_text(content, encoding="utf-8")
    return log_path


def build_run_log_content(result: PipelineResult) -> str:
    """Build Markdown content for a completed pipeline execution."""
    raw_file = format_optional_path(result.raw_file)
    raw_input_file = format_optional_path(result.raw_input_file)
    article_output = format_article_output(result.article_files)
    article_list = format_article_file_list(result.article_files)
    cost_report = format_cost_report(result.cost_report)
    sources = ", ".join(result.sources)
    step_sequence = format_step_sequence(result.steps)
    step_table = format_step_table(result)

    return (
        "# Agent 执行日志\n\n"
        "## 基本信息\n\n"
        f"- 运行 ID：`{result.run_id}`\n"
        "- 调用方：pipeline CLI\n"
        f"- 执行顺序：`{step_sequence}`\n"
        f"- 数据来源：`{sources}`\n"
        f"- 执行日期：`{result.started_at[:10]}`\n"
        f"- 开始时间：`{result.started_at}`\n"
        f"- 结束时间：`{result.finished_at}`\n"
        f"- 执行方式：`python pipeline/pipeline.py --sources {sources} "
        f"--limit {result.limit} {format_step_cli_args(result.steps)}`\n"
        f"- Dry-run：`{str(result.dry_run).lower()}`\n\n"
        "## 执行结果\n\n"
        "本次流程已完成选中的阶段：\n\n"
        f"{step_table}\n"
        "## collect 阶段\n\n"
        f"- 来源：`{sources}`\n"
        f"- 采集上限：{result.limit}\n"
        f"- 实际采集条目数：{result.collected_count}\n"
        f"- raw 输出：{raw_file}\n"
        f"- raw 输入：{raw_input_file}\n\n"
        "## analyze 阶段\n\n"
        "- 分析方式：调用 `pipeline/model_client.py` 中的 LLM 客户端\n"
        f"- 成功分析条目数：{result.analyzed_count}\n"
        f"{cost_report}\n"
        "## organize 阶段\n\n"
        "- 处理方式：按 `source_url` 去重，"
        "格式化为标准知识条目 JSON\n"
        f"- 跳过重复条目数：{result.skipped_duplicate_count}\n"
        f"- 待保存条目数：{result.saved_count}\n\n"
        "## save 阶段\n\n"
        f"- 保存目录：`{relative_path(ARTICLES_DIR)}`\n"
        f"- 保存条目数：{result.saved_count}\n"
        f"{article_list}\n"
        "## 验证记录\n\n"
        "- 已对保存的 article JSON 调用 `hooks/validate_json.py` 校验。\n"
        "- 每个保存条目均为独立 JSON 文件。\n"
        "- 条目状态默认为 `draft`，尚未进入正式分发条件。\n\n"
        "## 重要说明\n\n"
        "- 本次流程由本地 pipeline CLI 触发，"
        "不是定时任务自动触发。\n"
        "- Dry-run 模式不会写入 raw 或 article 文件。\n"
        "- 若外部 API 或 LLM provider 不可访问，"
        "流程会在对应阶段失败。\n"
    )


def format_step_sequence(steps: Sequence[int]) -> str:
    """Format selected steps as a readable execution sequence."""
    if not steps:
        return "未解析"
    return " -> ".join(STEP_NAMES.get(step, str(step)) for step in steps)


def format_step_cli_args(steps: Sequence[int]) -> str:
    """Format selected steps as CLI arguments."""
    if not steps or list(steps) == list(DEFAULT_STEPS):
        return ""
    return " ".join(f"--step {step}" for step in steps)


def format_step_table(result: PipelineResult) -> str:
    """Format the selected step table for a completed run log."""
    return (
        "| 阶段 | 步骤 | 输出 | 状态 |\n"
        "| --- | --- | --- | --- |\n"
        f"| 1 | collect | 候选条目 {result.collected_count} 条 | "
        f"{format_success_step_status(result.steps, 1)} |\n"
        f"| 2 | save_raw | {format_optional_path(result.raw_file)} | "
        f"{format_success_step_status(result.steps, 2)} |\n"
        f"| 3 | analyze_organize | 分析 {result.analyzed_count} 条，"
        f"跳过重复 {result.skipped_duplicate_count} 条 | "
        f"{format_success_step_status(result.steps, 3)} |\n"
        f"| 4 | save_articles | {format_article_output(result.article_files)} | "
        f"{format_success_step_status(result.steps, 4)} |\n"
    )


def format_success_step_status(steps: Sequence[int], step: int) -> str:
    """Return success table status for one step."""
    return "成功" if step in steps else "未执行"


def format_failure_step_status(steps: Sequence[int], step: int) -> str:
    """Return failure table status for one step."""
    return "失败或未完成" if step in steps else "未执行"


def format_optional_path(path: Path | None) -> str:
    """Format an optional path for Markdown tables."""
    if path is None:
        return "`未写入`"
    return f"`{relative_path(path)}`"


def format_article_output(article_files: Sequence[Path]) -> str:
    """Format article output summary for Markdown tables."""
    if not article_files:
        return "`未写入`"
    if len(article_files) == 1:
        return f"`{relative_path(article_files[0])}`"
    return f"`{relative_path(ARTICLES_DIR)}/*.json`"


def format_article_file_list(article_files: Sequence[Path]) -> str:
    """Format generated article file list for Markdown."""
    if not article_files:
        return "- 生成文件：无\n\n"

    lines = ["### article 输出摘要", ""]
    for path in article_files:
        lines.append(f"- `{relative_path(path)}`")
    lines.append("")
    return "\n".join(lines) + "\n"


def format_cost_report(report: dict[str, Any]) -> str:
    """Format the LLM cost report for Markdown logs."""
    provider_costs = report.get("provider_costs_cny", {})
    if isinstance(provider_costs, dict) and provider_costs:
        provider_cost_text = ", ".join(
            f"{provider}: {float(cost):.8f} 元"
            for provider, cost in sorted(provider_costs.items())
        )
    else:
        provider_cost_text = "无"

    return (
        f"- LLM 调用次数：{int(report.get('call_count', 0))}\n"
        f"- prompt tokens：{int(report.get('prompt_tokens', 0))}\n"
        f"- prompt cache hit tokens："
        f"{int(report.get('prompt_cache_hit_tokens', 0))}\n"
        f"- completion tokens：{int(report.get('completion_tokens', 0))}\n"
        f"- total tokens：{int(report.get('total_tokens', 0))}\n"
        f"- 估算用量次数：{int(report.get('estimated_call_count', 0))}\n"
        f"- 估算成本：`{float(report.get('estimated_cost_cny', 0.0)):.8f} 元`\n"
        f"- Provider 成本：`{provider_cost_text}`\n\n"
    )


def relative_path(path: Path) -> str:
    """Return repository-relative path for logs."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pipeline CLI."""
    args = parse_args(argv)
    configure_logging(args.verbose)
    run_id = build_run_id()
    started_at = now_iso()
    sources: list[str] = []
    steps: list[int] = []

    try:
        sources = parse_sources(args.sources)
        steps = parse_steps(args.steps)
        rss_feeds = parse_rss_feed_urls(args.rss_feeds)
        result = run_pipeline(
            sources=sources,
            limit=args.limit,
            rss_feeds=rss_feeds,
            steps=steps,
            dry_run=args.dry_run,
            run_id=run_id,
            started_at=started_at,
        )
    except Exception as exc:
        LOGGER.error("Pipeline failed: %s", exc)
        if args.verbose:
            LOGGER.exception("Pipeline failure details")
        try:
            log_path = write_failure_run_log(
                run_id=run_id,
                started_at=started_at,
                sources=sources,
                limit=args.limit,
                steps=steps,
                dry_run=args.dry_run,
                error=exc,
            )
            LOGGER.info("Failure run log: %s", log_path)
        except OSError as log_error:
            LOGGER.error("Failed to write failure run log: %s", log_error)
        return 1

    log_result(result, dry_run=args.dry_run)
    log_path = write_run_log(result)
    LOGGER.info("Run log: %s", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
