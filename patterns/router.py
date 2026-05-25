"""Router pattern for intent-based query handling."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Literal, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.model_client import LLMClientError, chat, chat_json  # noqa: E402


LOGGER = logging.getLogger(__name__)

Intent = Literal["github_search", "knowledge_query", "general_chat"]
Handler = Callable[[str], str]

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
KNOWLEDGE_ARTICLES_DIR = PROJECT_ROOT / "knowledge" / "articles"
KNOWLEDGE_INDEX_PATH = PROJECT_ROOT / "knowledge" / "articles" / "index.json"
DEFAULT_LIMIT = 5
REQUEST_TIMEOUT_SECONDS = 20

INTENTS: set[str] = {"github_search", "knowledge_query", "general_chat"}

GITHUB_KEYWORDS = (
    "github",
    "repo",
    "repository",
    "repositories",
    "star",
    "stars",
    "fork",
    "仓库",
    "开源项目",
    "代码库",
)
GITHUB_ACTION_KEYWORDS = (
    "搜索",
    "查找",
    "找",
    "search",
    "查询",
    "推荐",
)
KNOWLEDGE_KEYWORDS = (
    "知识库",
    "本地",
    "已收录",
    "收录",
    "条目",
    "文章",
    "数据库",
    "index",
    "knowledge",
)


def route(query: str) -> str:
    """Route a user query to the right intent handler.

    Args:
        query: User query text.

    Returns:
        Handler response text.
    """
    normalized_query = query.strip()
    if not normalized_query:
        return "请输入要处理的问题。"

    intent = classify_intent(normalized_query)
    handler = HANDLERS[intent]
    return handler(normalized_query)


def classify_intent(query: str) -> Intent:
    """Classify query intent using keywords first, then LLM fallback."""
    keyword_intent = classify_by_keyword(query)
    if keyword_intent is not None:
        return keyword_intent

    return classify_by_llm(query)


def classify_by_keyword(query: str) -> Intent | None:
    """Classify intent by zero-cost keyword rules."""
    normalized_query = query.casefold()

    if contains_any(normalized_query, KNOWLEDGE_KEYWORDS):
        return "knowledge_query"

    if contains_any(normalized_query, GITHUB_KEYWORDS) and contains_any(
        normalized_query,
        GITHUB_ACTION_KEYWORDS,
    ):
        return "github_search"

    return None


def classify_by_llm(query: str) -> Intent:
    """Classify ambiguous intent with an LLM fallback."""
    system_prompt = (
        "你是一个意图分类器。只返回 JSON 对象，不要返回解释。"
    )
    prompt = json.dumps(
        {
            "query": query,
            "allowed_intents": sorted(INTENTS),
            "rules": {
                "github_search": "用户想搜索 GitHub 仓库或开源项目。",
                "knowledge_query": "用户想查询本地知识库、已收录条目或文章。",
                "general_chat": "普通问答、闲聊或不需要工具的数据。",
            },
            "required_output": {"intent": "one allowed intent"},
        },
        ensure_ascii=False,
    )

    try:
        payload, _usage = chat_json(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=120,
        )
    except LLMClientError as exc:
        LOGGER.warning("LLM intent classification failed: %s", exc)
        return "general_chat"

    intent = payload.get("intent")
    if isinstance(intent, str) and intent in INTENTS:
        return intent  # type: ignore[return-value]

    LOGGER.warning("LLM returned invalid intent: %s", intent)
    return "general_chat"


def handle_github_search(query: str) -> str:
    """Search GitHub repositories with the GitHub Search API."""
    encoded_query = urllib.parse.quote(query, safe="")
    url = (
        f"{GITHUB_SEARCH_URL}?q={encoded_query}"
        f"&sort=updated&order=desc&per_page={DEFAULT_LIMIT}"
    )
    request = urllib.request.Request(
        url,
        headers=github_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return f"GitHub 搜索失败：HTTP {exc.code}。"
    except urllib.error.URLError as exc:
        return f"GitHub 搜索失败：{exc.reason}。"
    except json.JSONDecodeError:
        return "GitHub 搜索失败：返回内容不是有效 JSON。"

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return "GitHub 没有找到匹配的仓库。"

    lines = [f"GitHub 搜索结果：{query}"]
    for index, item in enumerate(items[:DEFAULT_LIMIT], start=1):
        if not isinstance(item, dict):
            continue
        full_name = safe_string(item.get("full_name")) or "unknown"
        html_url = safe_string(item.get("html_url"))
        description = safe_string(item.get("description")) or "无描述"
        stars = item.get("stargazers_count", 0)
        language = safe_string(item.get("language")) or "Unknown"
        lines.append(
            f"{index}. {full_name} ({language}, stars: {stars})\n"
            f"   {description}\n"
            f"   {html_url}"
        )

    return "\n".join(lines)


def handle_knowledge_query(query: str) -> str:
    """Search the local knowledge articles."""
    try:
        articles = load_knowledge_articles()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return f"知识库不可用：{exc}"

    matches = search_index_articles(query=query, articles=articles)
    if not matches:
        return "本地知识库未找到相关条目。"

    lines = [f"知识库检索结果：{query}"]
    for index, article in enumerate(matches[:DEFAULT_LIMIT], start=1):
        title = safe_string(article.get("title")) or "未命名条目"
        article_id = safe_string(article.get("id")) or "unknown"
        source_url = safe_string(article.get("source_url"))
        summary = safe_string(article.get("summary")) or "无摘要"
        tags = format_tags(article.get("tags"))
        lines.append(
            f"{index}. {title}\n"
            f"   id: {article_id}\n"
            f"   tags: {tags}\n"
            f"   summary: {summary}\n"
            f"   source: {source_url}"
        )

    return "\n".join(lines)


def handle_general_chat(query: str) -> str:
    """Answer a general query directly with an LLM."""
    system_prompt = (
        "你是一个简洁、准确的技术助手。"
        "如果问题需要实时数据而用户没有提供来源，请明确说明限制。"
    )
    try:
        text, _usage = chat(
            prompt=query,
            system_prompt=system_prompt,
            temperature=0.2,
        )
    except LLMClientError as exc:
        return f"LLM 调用失败：{exc}"
    return text


HANDLERS: dict[Intent, Handler] = {
    "github_search": handle_github_search,
    "knowledge_query": handle_knowledge_query,
    "general_chat": handle_general_chat,
}


def github_headers() -> dict[str, str]:
    """Build GitHub API request headers."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-knowledge-router",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    github_token = os.getenv("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    return headers


def load_knowledge_articles() -> list[dict[str, Any]]:
    """Load articles from index.json if present, else article JSON files."""
    if KNOWLEDGE_INDEX_PATH.exists():
        return load_knowledge_index()
    return load_article_json_files()


def load_knowledge_index() -> list[dict[str, Any]]:
    """Load knowledge/articles/index.json."""
    payload = json.loads(KNOWLEDGE_INDEX_PATH.read_text(encoding="utf-8"))

    if isinstance(payload, list):
        raw_articles = payload
    elif isinstance(payload, dict):
        raw_articles = payload.get("articles")
        if raw_articles is None and all(
            isinstance(value, dict) for value in payload.values()
        ):
            raw_articles = list(payload.values())
    else:
        raw_articles = None

    if not isinstance(raw_articles, list):
        raise ValueError(
            f"{KNOWLEDGE_INDEX_PATH} must be a list or contain articles list."
        )

    articles = [
        article
        for article in raw_articles
        if isinstance(article, dict)
    ]
    if not articles:
        raise ValueError(f"{KNOWLEDGE_INDEX_PATH} contains no articles.")

    return articles


def load_article_json_files() -> list[dict[str, Any]]:
    """Load article records by scanning knowledge/articles/*.json."""
    articles: list[dict[str, Any]] = []

    if not KNOWLEDGE_ARTICLES_DIR.exists():
        raise ValueError(
            f"articles directory does not exist: {KNOWLEDGE_ARTICLES_DIR}"
        )

    for path in sorted(KNOWLEDGE_ARTICLES_DIR.glob("*.json")):
        if path.name == KNOWLEDGE_INDEX_PATH.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Skipping unreadable article %s: %s", path, exc)
            continue

        if not isinstance(payload, dict):
            LOGGER.warning("Skipping non-object article JSON: %s", path)
            continue

        articles.append(normalize_article_record(payload, path))

    if not articles:
        raise ValueError(
            f"{KNOWLEDGE_ARTICLES_DIR} contains no article JSON files."
        )

    return articles


def normalize_article_record(
    article: dict[str, Any],
    path: Path,
) -> dict[str, Any]:
    """Normalize a scanned article record for lightweight search."""
    normalized_article = dict(article)
    normalized_article.setdefault("id", path.stem)
    normalized_article.setdefault("path", str(path.relative_to(PROJECT_ROOT)))
    return normalized_article


def search_index_articles(
    query: str,
    articles: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Search indexed articles by title, summary, content, and tags."""
    query_terms = tokenize(query)
    ranked_articles: list[tuple[int, dict[str, Any]]] = []

    for article in articles:
        score = score_article(query_terms=query_terms, article=article)
        if score > 0:
            ranked_articles.append((score, article))

    ranked_articles.sort(
        key=lambda item: (
            -item[0],
            safe_string(item[1].get("title")).casefold(),
        )
    )
    return [article for _score, article in ranked_articles]


def score_article(
    query_terms: Sequence[str],
    article: dict[str, Any],
) -> int:
    """Score one article against query terms."""
    title = safe_string(article.get("title")).casefold()
    summary = safe_string(article.get("summary")).casefold()
    content = safe_string(article.get("content")).casefold()
    tags_value = article.get("tags")
    tag_items = tags_value if isinstance(tags_value, list) else []
    tags = " ".join(
        safe_string(tag)
        for tag in tag_items
    ).casefold()

    score = 0
    for term in query_terms:
        term_value = term.casefold()
        score += title.count(term_value) * 5
        score += tags.count(term_value) * 4
        score += summary.count(term_value) * 3
        score += content.count(term_value)
    return score


def tokenize(query: str) -> list[str]:
    """Split query into simple searchable terms."""
    normalized_query = query.strip()
    terms = [
        term.strip()
        for term in normalized_query.replace("/", " ").split()
        if term.strip()
    ]
    if not terms:
        return [normalized_query]
    if len(terms) == 1:
        return terms
    return [normalized_query, *terms]


def contains_any(value: str, keywords: Sequence[str]) -> bool:
    """Return whether value contains any keyword."""
    return any(keyword.casefold() in value for keyword in keywords)


def safe_string(value: Any) -> str:
    """Return a string value or an empty string."""
    return value if isinstance(value, str) else ""


def format_tags(value: Any) -> str:
    """Format an article tags field."""
    if not isinstance(value, list):
        return "无"
    tags = [tag for tag in value if isinstance(tag, str) and tag.strip()]
    return ", ".join(tags) if tags else "无"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Route a query by intent.")
    parser.add_argument("query", nargs="*", help="Query text to route.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a small manual router test."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    query = " ".join(args.query).strip()
    if not query:
        LOGGER.error("Query is required.")
        return 1

    LOGGER.info("%s", route(query))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
