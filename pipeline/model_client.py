"""Unified LLM client for the knowledge pipeline."""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any, Literal, Sequence

import httpx


LOGGER = logging.getLogger(__name__)

DEFAULT_PROVIDER = "deepseek"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 1.0

ProviderName = Literal["deepseek", "qwen", "openai-codex"]
Message = dict[str, str]


@dataclass(frozen=True)
class Usage:
    """Token usage and estimated cost for an LLM response.

    Attributes:
        prompt_tokens: Number of prompt/input tokens.
        completion_tokens: Number of completion/output tokens.
        total_tokens: Total token count.
        estimated: Whether the token count was estimated locally rather than
            reported by the provider.
        cost_usd: Estimated USD cost for this request.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated: bool
    cost_usd: float


@dataclass(frozen=True)
class LLMResponse:
    """Normalized LLM response.

    Attributes:
        content: Assistant response content.
        usage: Normalized token usage and estimated cost.
        provider: Provider name used for the request.
        model: Model name used for the request.
    """

    content: str
    usage: Usage
    provider: str
    model: str


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration for an OpenAI-compatible provider."""

    name: ProviderName
    base_url: str
    api_key_env_names: tuple[str, ...]
    default_model: str
    input_usd_per_1m_tokens: float
    output_usd_per_1m_tokens: float


class LLMClientError(RuntimeError):
    """Raised when an LLM request cannot be completed."""


class LLMConfigurationError(LLMClientError):
    """Raised when LLM client configuration is invalid."""


class LLMProvider(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    def chat(
        self,
        messages: Sequence[Message],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send chat messages to the provider.

        Args:
            messages: Chat messages in OpenAI-compatible format.
            temperature: Sampling temperature.
            max_tokens: Optional maximum completion tokens.

        Returns:
            A normalized LLM response.
        """


class OpenAICompatibleProvider(LLMProvider):
    """LLM provider backed by an OpenAI-compatible chat completions API."""

    def __init__(
        self,
        config: ProviderConfig,
        api_key: str,
        model: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the provider.

        Args:
            config: Provider configuration.
            api_key: API key for the provider.
            model: Optional model override.
            timeout_seconds: HTTP request timeout in seconds.
        """
        self.config = config
        self.api_key = api_key
        self.model = model or config.default_model
        self.timeout_seconds = timeout_seconds

    def chat(
        self,
        messages: Sequence[Message],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send chat messages to an OpenAI-compatible endpoint."""
        validate_messages(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        response_json = self._post_chat_completions(payload)
        content = extract_chat_content(response_json)
        usage = build_usage(
            response_json=response_json,
            messages=messages,
            content=content,
            input_usd_per_1m_tokens=self.config.input_usd_per_1m_tokens,
            output_usd_per_1m_tokens=self.config.output_usd_per_1m_tokens,
        )

        return LLMResponse(
            content=content,
            usage=usage,
            provider=self.config.name,
            model=self.model,
        )

    def _post_chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a request to the provider chat completions endpoint."""
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict):
            raise LLMClientError("LLM provider returned a non-object JSON body.")

        return data


PROVIDER_CONFIGS: dict[ProviderName, ProviderConfig] = {
    "deepseek": ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key_env_names=("DEEPSEEK_API_KEY",),
        default_model="deepseek-chat",
        input_usd_per_1m_tokens=0.27,
        output_usd_per_1m_tokens=1.10,
    ),
    "qwen": ProviderConfig(
        name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env_names=("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        default_model="qwen-plus",
        input_usd_per_1m_tokens=0.40,
        output_usd_per_1m_tokens=1.20,
    ),
    "openai-codex": ProviderConfig(
        name="openai-codex",
        base_url="https://api.openai.com/v1",
        api_key_env_names=("OPENAI_CODEX_API_KEY", "OPENAI_API_KEY"),
        default_model="gpt-5-codex",
        input_usd_per_1m_tokens=1.25,
        output_usd_per_1m_tokens=10.00,
    ),
}


PROVIDER_ALIASES = {
    "deepseek": "deepseek",
    "qwen": "qwen",
    "dashscope": "qwen",
    "openai": "openai-codex",
    "openai_codex": "openai-codex",
    "openai-codex": "openai-codex",
    "codex": "openai-codex",
}


def create_provider(
    provider_name: str | None = None,
    model: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> LLMProvider:
    """Create an LLM provider from environment configuration.

    Args:
        provider_name: Optional provider override. Defaults to
            `LLM_PROVIDER`, then `deepseek`.
        model: Optional model override. Defaults to `LLM_MODEL`, then the
            provider default.
        timeout_seconds: HTTP request timeout in seconds.

    Returns:
        A configured LLM provider.

    Raises:
        LLMConfigurationError: If the provider name or API key is missing.
    """
    normalized_name = normalize_provider_name(
        provider_name or os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER)
    )
    config = apply_pricing_overrides(PROVIDER_CONFIGS[normalized_name])
    api_key = read_api_key(config)
    configured_model = model or os.getenv("LLM_MODEL") or config.default_model

    return OpenAICompatibleProvider(
        config=config,
        api_key=api_key,
        model=configured_model,
        timeout_seconds=timeout_seconds,
    )


def normalize_provider_name(provider_name: str) -> ProviderName:
    """Normalize provider names and aliases."""
    normalized_name = provider_name.strip().lower()
    provider = PROVIDER_ALIASES.get(normalized_name)
    if provider is None:
        supported = ", ".join(sorted(PROVIDER_CONFIGS))
        raise LLMConfigurationError(
            f"Unsupported LLM provider '{provider_name}'. "
            f"Supported providers: {supported}."
        )
    return provider  # type: ignore[return-value]


def read_api_key(config: ProviderConfig) -> str:
    """Read provider API key from environment variables."""
    for env_name in config.api_key_env_names:
        api_key = os.getenv(env_name)
        if api_key:
            return api_key

    expected = " or ".join(config.api_key_env_names)
    raise LLMConfigurationError(
        f"Missing API key for provider '{config.name}'. "
        f"Set {expected}."
    )


def apply_pricing_overrides(config: ProviderConfig) -> ProviderConfig:
    """Apply optional pricing overrides from environment variables."""
    prefix = config.name.upper().replace("-", "_")
    input_price = read_float_env(
        f"{prefix}_INPUT_USD_PER_1M",
        "LLM_INPUT_USD_PER_1M",
        default=config.input_usd_per_1m_tokens,
    )
    output_price = read_float_env(
        f"{prefix}_OUTPUT_USD_PER_1M",
        "LLM_OUTPUT_USD_PER_1M",
        default=config.output_usd_per_1m_tokens,
    )
    return replace(
        config,
        input_usd_per_1m_tokens=input_price,
        output_usd_per_1m_tokens=output_price,
    )


def read_float_env(*env_names: str, default: float) -> float:
    """Read a float from the first configured environment variable."""
    for env_name in env_names:
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue

        try:
            return float(raw_value)
        except ValueError as exc:
            raise LLMConfigurationError(
                f"Environment variable {env_name} must be a float."
            ) from exc

    return default


def chat_with_retry(
    messages: Sequence[Message],
    provider: LLMProvider | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> LLMResponse:
    """Call an LLM with retry and exponential backoff.

    Args:
        messages: Chat messages in OpenAI-compatible format.
        provider: Optional provider instance. If omitted, one is created from
            environment variables.
        temperature: Sampling temperature.
        max_tokens: Optional maximum completion tokens.
        max_retries: Total attempts, including the first attempt.
        backoff_seconds: Initial exponential backoff delay.

    Returns:
        A normalized LLM response.

    Raises:
        LLMClientError: If all retry attempts fail.
    """
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1.")

    llm_provider = provider or create_provider()
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            return llm_provider.chat(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            last_error = exc
            if not should_retry(exc) or attempt == max_retries:
                break

            delay_seconds = backoff_seconds * (2 ** (attempt - 1))
            LOGGER.warning(
                "LLM request failed on attempt %s/%s; retrying in %.1fs: %s",
                attempt,
                max_retries,
                delay_seconds,
                exc,
            )
            time.sleep(delay_seconds)

    raise LLMClientError(
        f"LLM request failed after {max_retries} attempts."
    ) from last_error


def quick_chat(
    prompt: str,
    system_prompt: str | None = None,
    provider: LLMProvider | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> LLMResponse:
    """Send one user prompt to an LLM.

    Args:
        prompt: User prompt.
        system_prompt: Optional system prompt.
        provider: Optional provider instance.
        temperature: Sampling temperature.
        max_tokens: Optional maximum completion tokens.

    Returns:
        A normalized LLM response.
    """
    messages: list[Message] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    return chat_with_retry(
        messages=messages,
        provider=provider,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def should_retry(error: Exception) -> bool:
    """Return whether an exception is retryable."""
    if isinstance(error, (httpx.TimeoutException, httpx.TransportError)):
        return True

    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 409, 429, 500, 502, 503, 504}

    return False


def validate_messages(messages: Sequence[Message]) -> None:
    """Validate chat message format."""
    if not messages:
        raise ValueError("messages must not be empty.")

    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"}:
            raise ValueError(f"messages[{index}].role is invalid.")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"messages[{index}].content must be non-empty.")


def extract_chat_content(response_json: dict[str, Any]) -> str:
    """Extract assistant text from a chat completions response."""
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMClientError("LLM response does not contain choices.")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise LLMClientError("LLM response choice is invalid.")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise LLMClientError("LLM response choice does not contain a message.")

    content = message.get("content")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return extract_text_from_content_parts(content)

    raise LLMClientError("LLM response message content is invalid.")


def extract_text_from_content_parts(parts: Sequence[Any]) -> str:
    """Extract text from multimodal content parts."""
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            text_parts.append(text)
    return "\n".join(text_parts)


def build_usage(
    response_json: dict[str, Any],
    messages: Sequence[Message],
    content: str,
    input_usd_per_1m_tokens: float,
    output_usd_per_1m_tokens: float,
) -> Usage:
    """Build normalized usage from provider response or local estimates."""
    usage_payload = response_json.get("usage")
    if isinstance(usage_payload, dict):
        prompt_tokens = int(usage_payload.get("prompt_tokens") or 0)
        completion_tokens = int(usage_payload.get("completion_tokens") or 0)
        total_tokens = int(
            usage_payload.get("total_tokens")
            or prompt_tokens + completion_tokens
        )
        estimated = False
    else:
        prompt_tokens = estimate_tokens_for_messages(messages)
        completion_tokens = estimate_tokens(content)
        total_tokens = prompt_tokens + completion_tokens
        estimated = True

    cost_usd = calculate_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_usd_per_1m_tokens=input_usd_per_1m_tokens,
        output_usd_per_1m_tokens=output_usd_per_1m_tokens,
    )

    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated=estimated,
        cost_usd=cost_usd,
    )


def estimate_tokens_for_messages(messages: Sequence[Message]) -> int:
    """Estimate token count for chat messages."""
    per_message_overhead = 4
    return sum(
        estimate_tokens(message.get("content", "")) + per_message_overhead
        for message in messages
    )


def estimate_tokens(text: str) -> int:
    """Estimate token count for mixed Chinese and English text.

    The estimate is intentionally simple: CJK characters are counted close to
    one token each, while other non-whitespace characters are counted at about
    four characters per token.
    """
    if not text:
        return 0

    cjk_chars = 0
    other_chars = 0
    for char in text:
        if char.isspace():
            continue
        if is_cjk_char(char):
            cjk_chars += 1
        else:
            other_chars += 1

    return max(1, cjk_chars + math.ceil(other_chars / 4))


def is_cjk_char(char: str) -> bool:
    """Return whether a character is in a common CJK Unicode block."""
    code_point = ord(char)
    return (
        0x4E00 <= code_point <= 0x9FFF
        or 0x3400 <= code_point <= 0x4DBF
        or 0x3040 <= code_point <= 0x30FF
        or 0xAC00 <= code_point <= 0xD7AF
    )


def calculate_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    input_usd_per_1m_tokens: float,
    output_usd_per_1m_tokens: float,
) -> float:
    """Calculate estimated request cost in USD."""
    input_cost = (prompt_tokens / 1_000_000) * input_usd_per_1m_tokens
    output_cost = (completion_tokens / 1_000_000) * output_usd_per_1m_tokens
    return round(input_cost + output_cost, 8)


def main() -> int:
    """Run a small manual smoke test against the configured provider."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    prompt = " ".join(sys.argv[1:]) or "用一句话说明你能做什么。"

    try:
        response = quick_chat(prompt)
    except LLMClientError as exc:
        LOGGER.error("LLM request failed: %s", exc)
        return 1

    LOGGER.info("provider=%s model=%s", response.provider, response.model)
    LOGGER.info("content=%s", response.content)
    LOGGER.info(
        "usage prompt=%s completion=%s total=%s estimated=%s cost_usd=%.8f",
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
        response.usage.total_tokens,
        response.usage.estimated,
        response.usage.cost_usd,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
