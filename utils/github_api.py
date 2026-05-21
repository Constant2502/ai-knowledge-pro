"""Utilities for reading basic repository metadata from the GitHub API."""

from __future__ import annotations

import json
from typing import Any, TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GITHUB_API_BASE_URL = "https://api.github.com"


class GitHubApiError(RuntimeError):
    """Raised when a GitHub API request fails."""


class GitHubRepoInfo(TypedDict):
    """Basic GitHub repository information used by the knowledge pipeline."""

    stars: int
    forks: int
    description: str | None


def get_repo_basic_info(
    repo_full_name: str,
    token: str | None = None,
    timeout: float = 10.0,
) -> GitHubRepoInfo:
    """Fetch basic information for a GitHub repository.

    Args:
        repo_full_name: Repository name in `owner/repo` format.
        token: Optional GitHub token. Use this to increase rate limits without
            hard-coding secrets in the repository.
        timeout: Request timeout in seconds.

    Returns:
        A dictionary containing the repository star count, fork count, and
        description.

    Raises:
        ValueError: If `repo_full_name` is not in `owner/repo` format.
        GitHubApiError: If the GitHub API request fails or returns invalid JSON.
    """
    normalized_repo = repo_full_name.strip()
    if "/" not in normalized_repo or normalized_repo.count("/") != 1:
        raise ValueError("repo_full_name must use the 'owner/repo' format.")

    url = f"{GITHUB_API_BASE_URL}/repos/{normalized_repo}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-knowledge-pro",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, headers=headers, method="GET")

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise GitHubApiError(
            f"GitHub API request failed with status {exc.code}."
        ) from exc
    except URLError as exc:
        raise GitHubApiError("GitHub API request failed.") from exc
    except json.JSONDecodeError as exc:
        raise GitHubApiError("GitHub API returned invalid JSON.") from exc

    return _parse_repo_info(payload)


def _parse_repo_info(payload: dict[str, Any]) -> GitHubRepoInfo:
    """Extract the repository fields required by this project."""
    return {
        "stars": int(payload.get("stargazers_count", 0)),
        "forks": int(payload.get("forks_count", 0)),
        "description": payload.get("description"),
    }
