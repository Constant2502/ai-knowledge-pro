#!/usr/bin/env python3
"""MCP server for searching the local knowledge article database."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO


DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ai-knowledge-server"
SERVER_VERSION = "0.1.0"
MAX_SEARCH_LIMIT = 50

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArticleRecord:
    """A parsed article with its source file path."""

    path: Path
    data: dict[str, Any]


@dataclass(frozen=True)
class ArticleLoadResult:
    """Result of loading articles from disk."""

    articles: list[ArticleRecord] = field(default_factory=list)
    invalid_files: list[dict[str, str]] = field(default_factory=list)


class RpcError(Exception):
    """JSON-RPC error with a code and message."""

    def __init__(
        self,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> None:
        """Initialize the JSON-RPC error."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ToolError(Exception):
    """User-facing MCP tool execution error."""


class KnowledgeRepository:
    """Read-only repository for local knowledge article JSON files."""

    def __init__(self, articles_dir: Path) -> None:
        """Initialize the repository.

        Args:
            articles_dir: Directory containing article JSON files.
        """
        self.articles_dir = articles_dir

    def load_articles(self) -> ArticleLoadResult:
        """Load all parseable JSON object articles from the articles directory."""
        articles: list[ArticleRecord] = []
        invalid_files: list[dict[str, str]] = []

        if not self.articles_dir.exists():
            return ArticleLoadResult(
                invalid_files=[
                    {
                        "path": str(self.articles_dir),
                        "error": "articles directory does not exist",
                    }
                ]
            )

        for path in sorted(self.articles_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                invalid_files.append({"path": str(path), "error": str(exc)})
                continue
            except json.JSONDecodeError as exc:
                invalid_files.append(
                    {"path": str(path), "error": f"invalid JSON: {exc}"}
                )
                continue

            if not isinstance(data, dict):
                invalid_files.append(
                    {
                        "path": str(path),
                        "error": "top-level JSON value is not an object",
                    }
                )
                continue

            articles.append(ArticleRecord(path=path, data=data))

        return ArticleLoadResult(articles=articles, invalid_files=invalid_files)

    def search_articles(
        self,
        keyword: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search articles by keyword in title and summary.

        Args:
            keyword: Keyword or phrase to search.
            limit: Maximum number of matched articles to return.

        Returns:
            A dictionary containing matched article summaries.
        """
        keyword = keyword.strip()
        if not keyword:
            raise ToolError("keyword must not be empty")

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ToolError("limit must be an integer")

        if limit < 1:
            raise ToolError("limit must be greater than or equal to 1")

        limit = min(limit, MAX_SEARCH_LIMIT)
        load_result = self.load_articles()
        matches: list[tuple[int, ArticleRecord]] = []

        for article in load_result.articles:
            rank = score_article_match(article.data, keyword)
            if rank > 0:
                matches.append((rank, article))

        matches.sort(
            key=lambda item: (
                -item[0],
                safe_string(item[1].data.get("title")).lower(),
                safe_string(item[1].data.get("id")),
            )
        )

        return {
            "keyword": keyword,
            "limit": limit,
            "total_matches": len(matches),
            "articles": [
                article_search_result(article, rank)
                for rank, article in matches[:limit]
            ],
            "invalid_files": load_result.invalid_files,
        }

    def get_article(self, article_id: str) -> dict[str, Any]:
        """Get a full article by its ID.

        Args:
            article_id: Article ID to look up.

        Returns:
            The full article JSON object plus its file path.
        """
        article_id = article_id.strip()
        if not article_id:
            raise ToolError("article_id must not be empty")

        load_result = self.load_articles()
        for article in load_result.articles:
            if article.data.get("id") == article_id:
                return {
                    "path": str(article.path),
                    "article": article.data,
                }

        raise ToolError(f"article not found: {article_id}")

    def knowledge_stats(self) -> dict[str, Any]:
        """Return article count, source distribution, and popular tags."""
        load_result = self.load_articles()
        source_counter: Counter[str] = Counter()
        tag_counter: Counter[str] = Counter()

        for article in load_result.articles:
            source = safe_string(article.data.get("source")) or "unknown"
            source_counter[source] += 1

            tags = article.data.get("tags")
            if isinstance(tags, list):
                for tag in tags:
                    tag_text = safe_string(tag).strip()
                    if tag_text:
                        tag_counter[tag_text] += 1

        return {
            "article_total": len(load_result.articles),
            "source_distribution": dict(
                sorted(source_counter.items(), key=lambda item: item[0])
            ),
            "popular_tags": [
                {"tag": tag, "count": count}
                for tag, count in tag_counter.most_common(20)
            ],
            "invalid_file_total": len(load_result.invalid_files),
            "invalid_files": load_result.invalid_files,
        }


class KnowledgeMcpServer:
    """Small JSON-RPC MCP server exposing local knowledge tools."""

    def __init__(self, repository: KnowledgeRepository) -> None:
        """Initialize the server."""
        self.repository = repository

    def handle_message(self, message: Any) -> Any | None:
        """Handle a JSON-RPC message or batch."""
        if isinstance(message, list):
            responses = [
                response
                for item in message
                if (response := self.handle_single_message(item)) is not None
            ]
            return responses if responses else None

        return self.handle_single_message(message)

    def handle_single_message(self, message: Any) -> dict[str, Any] | None:
        """Handle one JSON-RPC request or notification."""
        request_id = None
        is_notification = True

        try:
            if not isinstance(message, dict):
                raise RpcError(-32600, "Invalid Request")

            request_id = message.get("id")
            is_notification = "id" not in message

            if message.get("jsonrpc") != "2.0":
                raise RpcError(-32600, "Invalid Request")

            method = message.get("method")
            if not isinstance(method, str):
                raise RpcError(-32600, "Invalid Request")

            params = message.get("params", {})
            result = self.dispatch(method, params)
        except RpcError as exc:
            if is_notification:
                return None
            return build_error_response(request_id, exc)
        except Exception as exc:  # noqa: BLE001 - keep protocol loop alive.
            LOGGER.exception("Unhandled server error")
            if is_notification:
                return None
            return build_error_response(
                request_id,
                RpcError(-32603, "Internal error", {"error": str(exc)}),
            )

        if is_notification:
            return None

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }

    def dispatch(self, method: str, params: Any) -> dict[str, Any]:
        """Dispatch a JSON-RPC method."""
        if method == "initialize":
            return self.initialize(params)

        if method == "tools/list":
            return {"tools": tool_definitions()}

        if method == "tools/call":
            return self.call_tool(params)

        if method == "ping":
            return {}

        if method == "shutdown":
            return {}

        raise RpcError(-32601, f"Method not found: {method}")

    def initialize(self, params: Any) -> dict[str, Any]:
        """Return MCP server capabilities."""
        protocol_version = DEFAULT_PROTOCOL_VERSION
        if isinstance(params, dict):
            requested_version = params.get("protocolVersion")
            if isinstance(requested_version, str) and requested_version:
                protocol_version = requested_version

        return {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        }

    def call_tool(self, params: Any) -> dict[str, Any]:
        """Call one of the supported MCP tools."""
        if not isinstance(params, dict):
            raise RpcError(-32602, "Invalid params: expected object")

        tool_name = params.get("name")
        if not isinstance(tool_name, str):
            raise RpcError(-32602, "Invalid params: missing tool name")

        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "Invalid params: arguments must be object")

        try:
            result = self.execute_tool(tool_name, arguments)
        except ToolError as exc:
            return tool_error_response(str(exc))

        return tool_success_response(result)

    def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a supported tool by name."""
        if tool_name == "search_articles":
            keyword = arguments.get("keyword")
            if not isinstance(keyword, str):
                raise ToolError("keyword is required and must be a string")

            limit = arguments.get("limit", 5)
            return self.repository.search_articles(keyword=keyword, limit=limit)

        if tool_name == "get_article":
            article_id = arguments.get("article_id")
            if not isinstance(article_id, str):
                raise ToolError("article_id is required and must be a string")

            return self.repository.get_article(article_id=article_id)

        if tool_name == "knowledge_stats":
            return self.repository.knowledge_stats()

        raise ToolError(f"unknown tool: {tool_name}")


def tool_definitions() -> list[dict[str, Any]]:
    """Return MCP tool metadata."""
    return [
        {
            "name": "search_articles",
            "description": "Search local knowledge articles by keyword in "
            "title and summary.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword or phrase to search.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT,
                        "default": 5,
                        "description": "Maximum number of results to return.",
                    },
                },
                "required": ["keyword"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_article",
            "description": "Get a full local knowledge article by article ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "article_id": {
                        "type": "string",
                        "description": "Article ID to retrieve.",
                    }
                },
                "required": ["article_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "knowledge_stats",
            "description": "Return local knowledge base statistics.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def score_article_match(article: dict[str, Any], keyword: str) -> int:
    """Compute a simple rank score for a keyword match."""
    terms = keyword_terms(keyword)
    title = safe_string(article.get("title")).lower()
    summary = safe_string(article.get("summary")).lower()
    rank = 0

    for term in terms:
        term_lower = term.lower()
        title_hits = title.count(term_lower)
        summary_hits = summary.count(term_lower)
        rank += title_hits * 3 + summary_hits

    return rank


def keyword_terms(keyword: str) -> list[str]:
    """Split a keyword into searchable terms while preserving phrases."""
    stripped_keyword = keyword.strip()
    terms = [
        term
        for term in re.split(r"\s+", stripped_keyword)
        if term
    ]

    if len(terms) <= 1:
        return [stripped_keyword]

    return [stripped_keyword, *terms]


def article_search_result(
    article: ArticleRecord,
    rank: int,
) -> dict[str, Any]:
    """Build a compact search result object."""
    data = article.data
    return {
        "id": data.get("id", ""),
        "title": data.get("title", ""),
        "source": data.get("source", ""),
        "source_url": data.get("source_url", ""),
        "summary": data.get("summary", ""),
        "score": data.get("score"),
        "tags": data.get("tags", []),
        "match_score": rank,
        "path": str(article.path),
    }


def safe_string(value: Any) -> str:
    """Convert a JSON value into a safe string for search and stats."""
    return value if isinstance(value, str) else ""


def tool_success_response(result: Any) -> dict[str, Any]:
    """Build a successful MCP tools/call response."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ],
        "isError": False,
    }


def tool_error_response(message: str) -> dict[str, Any]:
    """Build a failed MCP tools/call response."""
    return {
        "content": [
            {
                "type": "text",
                "text": message,
            }
        ],
        "isError": True,
    }


def build_error_response(
    request_id: Any,
    error: RpcError,
) -> dict[str, Any]:
    """Build a JSON-RPC error response."""
    error_body: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
    }
    if error.data is not None:
        error_body["data"] = error.data

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error_body,
    }


def read_message(stdin: BinaryIO) -> Any | None:
    """Read one JSON-RPC message from stdio.

    MCP stdio normally uses Content-Length framing. This function also accepts
    newline-delimited JSON to make simple local testing easier.
    """
    first_line = stdin.readline()
    while first_line in {b"\n", b"\r\n"}:
        first_line = stdin.readline()

    if not first_line:
        return None

    if looks_like_header(first_line):
        return read_framed_message(stdin, first_line)

    return json.loads(first_line.decode("utf-8"))


def looks_like_header(line: bytes) -> bool:
    """Return whether the first line appears to be an MCP stdio header."""
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return False

    return bool(re.fullmatch(r"[A-Za-z0-9-]+:\s*.*\r?\n", text))


def read_framed_message(stdin: BinaryIO, first_line: bytes) -> Any:
    """Read one Content-Length-framed JSON-RPC message."""
    headers: dict[str, str] = {}
    line = first_line

    while line not in {b"\n", b"\r\n", b""}:
        try:
            text = line.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("header is not valid ASCII") from exc

        if ":" not in text:
            raise ValueError(f"invalid header line: {text}")

        key, value = text.split(":", 1)
        headers[key.lower()] = value.strip()
        line = stdin.readline()

    if line == b"":
        raise ValueError("unexpected EOF while reading headers")

    content_length = headers.get("content-length")
    if content_length is None:
        raise ValueError("missing Content-Length header")

    try:
        length = int(content_length)
    except ValueError as exc:
        raise ValueError("Content-Length must be an integer") from exc

    if length < 0:
        raise ValueError("Content-Length must be non-negative")

    body = stdin.read(length)
    if len(body) != length:
        raise ValueError("unexpected EOF while reading message body")

    return json.loads(body.decode("utf-8"))


def write_message(stdout: BinaryIO, message: Any) -> None:
    """Write one JSON-RPC message with MCP stdio framing."""
    body = json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stdout.write(header)
    stdout.write(body)
    stdout.flush()


def configure_logging() -> None:
    """Configure stderr logging for server diagnostics."""
    level_name = os.getenv("MCP_KNOWLEDGE_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def articles_dir_from_env() -> Path:
    """Return the article directory, allowing an environment override."""
    configured_dir = os.getenv("KNOWLEDGE_ARTICLES_DIR")
    if configured_dir:
        return Path(configured_dir).expanduser().resolve()

    return Path(__file__).resolve().parent / "knowledge" / "articles"


def run_server() -> None:
    """Run the MCP server event loop."""
    configure_logging()
    repository = KnowledgeRepository(articles_dir=articles_dir_from_env())
    server = KnowledgeMcpServer(repository=repository)

    while True:
        try:
            message = read_message(sys.stdin.buffer)
        except json.JSONDecodeError as exc:
            write_message(
                sys.stdout.buffer,
                build_error_response(
                    None,
                    RpcError(-32700, "Parse error", {"error": str(exc)}),
                ),
            )
            continue
        except ValueError as exc:
            write_message(
                sys.stdout.buffer,
                build_error_response(
                    None,
                    RpcError(-32700, "Parse error", {"error": str(exc)}),
                ),
            )
            continue

        if message is None:
            break

        response = server.handle_message(message)
        if response is not None:
            write_message(sys.stdout.buffer, response)


if __name__ == "__main__":
    run_server()
