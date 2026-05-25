"""Unified LLM client for the knowledge pipeline."""

from __future__ import annotations

import json
import logging
import math
import os
import re
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
        prompt_cache_hit_tokens: Number of input tokens served from cache.
        prompt_cache_miss_tokens: Number of input tokens not served from cache.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated: bool
    cost_usd: float
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0


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


@dataclass(frozen=True)
class TokenPricing:
    """Per-million-token pricing in CNY."""

    input_cny_per_1m_tokens: float
    output_cny_per_1m_tokens: float
    cached_input_cny_per_1m_tokens: float | None = None


@dataclass(frozen=True)
class CostRecord:
    """One tracked LLM API call cost record."""

    provider: str
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int
    estimated: bool
    cost_cny: float


DEFAULT_USD_TO_CNY_RATE = 7.2

# Public pricing checked on 2026-05-24. OpenAI USD prices are converted
# with DEFAULT_USD_TO_CNY_RATE for local CNY reporting.
DEFAULT_CNY_PRICING_BY_PROVIDER: dict[str, TokenPricing] = {
    "deepseek": TokenPricing(
        input_cny_per_1m_tokens=1.0,
        output_cny_per_1m_tokens=2.0,
        cached_input_cny_per_1m_tokens=0.02,
    ),
    "qwen": TokenPricing(
        input_cny_per_1m_tokens=0.8,
        output_cny_per_1m_tokens=2.0,
    ),
    "openai": TokenPricing(
        input_cny_per_1m_tokens=round(1.75 * DEFAULT_USD_TO_CNY_RATE, 6),
        output_cny_per_1m_tokens=round(14.0 * DEFAULT_USD_TO_CNY_RATE, 6),
        cached_input_cny_per_1m_tokens=round(
            0.175 * DEFAULT_USD_TO_CNY_RATE,
            6,
        ),
    ),
}

DEFAULT_CNY_PRICING_BY_MODEL: dict[str, TokenPricing] = {
    "deepseek-chat": DEFAULT_CNY_PRICING_BY_PROVIDER["deepseek"],
    "deepseek-v4-flash": DEFAULT_CNY_PRICING_BY_PROVIDER["deepseek"],
    "qwen-plus": DEFAULT_CNY_PRICING_BY_PROVIDER["qwen"],
    "qwen-plus-latest": DEFAULT_CNY_PRICING_BY_PROVIDER["qwen"],
    "gpt-5-codex": TokenPricing(
        input_cny_per_1m_tokens=round(1.25 * DEFAULT_USD_TO_CNY_RATE, 6),
        output_cny_per_1m_tokens=round(10.0 * DEFAULT_USD_TO_CNY_RATE, 6),
        cached_input_cny_per_1m_tokens=round(
            0.125 * DEFAULT_USD_TO_CNY_RATE,
            6,
        ),
    ),
    "gpt-5.2-codex": DEFAULT_CNY_PRICING_BY_PROVIDER["openai"],
    "gpt-5.3-codex": DEFAULT_CNY_PRICING_BY_PROVIDER["openai"],
}

COST_PROVIDER_ALIASES = {
    "deepseek": "deepseek",
    "qwen": "qwen",
    "dashscope": "qwen",
    "openai": "openai",
    "openai-codex": "openai",
    "openai_codex": "openai",
    "codex": "openai",
}


class CostTracker:
    """Track LLM token usage and estimated CNY cost."""

    def __init__(
        self,
        pricing_by_provider: dict[str, TokenPricing] | None = None,
        pricing_by_model: dict[str, TokenPricing] | None = None,
    ) -> None:
        """Initialize the cost tracker.

        Args:
            pricing_by_provider: Optional provider-level price table.
            pricing_by_model: Optional model-level price table.
        """
        self.pricing_by_provider = dict(
            pricing_by_provider or DEFAULT_CNY_PRICING_BY_PROVIDER
        )
        self.pricing_by_model = dict(
            pricing_by_model or DEFAULT_CNY_PRICING_BY_MODEL
        )
        self.records: list[CostRecord] = []

    def record(
        self,
        usage: Usage,
        provider: str,
        model: str | None = None,
    ) -> None:
        """Record one successful LLM API call.

        Args:
            usage: Normalized token usage.
            provider: Provider name or alias.
            model: Optional model name for more precise pricing.
        """
        provider_key = normalize_cost_provider(provider)
        pricing = self.pricing_for(provider_key=provider_key, model=model)
        cost_cny = calculate_cost_cny(
            usage=usage,
            pricing=pricing,
        )
        self.records.append(
            CostRecord(
                provider=provider_key,
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                prompt_cache_hit_tokens=usage.prompt_cache_hit_tokens,
                prompt_cache_miss_tokens=usage.prompt_cache_miss_tokens,
                estimated=usage.estimated,
                cost_cny=cost_cny,
            )
        )

    def estimated_cost(self, provider: str | None = None) -> float:
        """Return estimated accumulated cost in CNY.

        Args:
            provider: Optional provider name or alias. If omitted, all
                providers are included.

        Returns:
            Estimated cost in CNY.
        """
        records = self.matching_records(provider)
        return round(sum(record.cost_cny for record in records), 8)

    def report(self, provider: str | None = None) -> dict[str, Any]:
        """Log and return a cost report.

        Args:
            provider: Optional provider name or alias. If omitted, all
                providers are included.

        Returns:
            A structured report dictionary.
        """
        records = self.matching_records(provider)
        report = build_cost_report(records)
        self.log_report(report=report, provider=provider)
        return report

    def reset(self) -> None:
        """Clear all tracked cost records."""
        self.records.clear()

    def matching_records(self, provider: str | None) -> list[CostRecord]:
        """Return records filtered by optional provider."""
        if provider is None:
            return list(self.records)

        provider_key = normalize_cost_provider(provider)
        return [
            record
            for record in self.records
            if record.provider == provider_key
        ]

    def pricing_for(
        self,
        provider_key: str,
        model: str | None,
    ) -> TokenPricing:
        """Return model-specific pricing if available, else provider pricing."""
        if model:
            model_key = model.strip().lower()
            model_pricing = self.pricing_by_model.get(model_key)
            if model_pricing is not None:
                return model_pricing

        try:
            return self.pricing_by_provider[provider_key]
        except KeyError as exc:
            raise ValueError(
                f"No CNY pricing configured for provider '{provider_key}'."
            ) from exc

    def log_report(self, report: dict[str, Any], provider: str | None) -> None:
        """Log a compact cost report."""
        label = provider or "all"
        LOGGER.info(
            "LLM cost report provider=%s calls=%s prompt_tokens=%s "
            "cache_hit_tokens=%s completion_tokens=%s estimated_calls=%s "
            "estimated_cost_cny=%.8f",
            label,
            report["call_count"],
            report["prompt_tokens"],
            report["prompt_cache_hit_tokens"],
            report["completion_tokens"],
            report["estimated_call_count"],
            report["estimated_cost_cny"],
        )


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

        llm_response = LLMResponse(
            content=content,
            usage=usage,
            provider=self.config.name,
            model=self.model,
        )
        try:
            tracker.record(
                usage=usage,
                provider=llm_response.provider,
                model=llm_response.model,
            )
        except ValueError as exc:
            LOGGER.warning("Failed to record LLM cost: %s", exc)

        return llm_response

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


tracker = CostTracker()


def normalize_cost_provider(provider: str) -> str:
    """Normalize provider names for cost tracking."""
    normalized_provider = provider.strip().lower()
    provider_key = COST_PROVIDER_ALIASES.get(normalized_provider)
    if provider_key is None:
        supported = ", ".join(sorted(DEFAULT_CNY_PRICING_BY_PROVIDER))
        raise ValueError(
            f"Unsupported cost provider '{provider}'. "
            f"Supported providers: {supported}."
        )
    return provider_key


def calculate_cost_cny(usage: Usage, pricing: TokenPricing) -> float:
    """Calculate estimated request cost in CNY."""
    cache_hit_tokens = usage.prompt_cache_hit_tokens
    cache_miss_tokens = usage.prompt_cache_miss_tokens

    if cache_hit_tokens <= 0 and cache_miss_tokens <= 0:
        cache_miss_tokens = usage.prompt_tokens

    if pricing.cached_input_cny_per_1m_tokens is None:
        input_cost = (
            usage.prompt_tokens / 1_000_000
        ) * pricing.input_cny_per_1m_tokens
    else:
        input_cost = (
            cache_miss_tokens / 1_000_000
        ) * pricing.input_cny_per_1m_tokens
        input_cost += (
            cache_hit_tokens / 1_000_000
        ) * pricing.cached_input_cny_per_1m_tokens

    output_cost = (
        usage.completion_tokens / 1_000_000
    ) * pricing.output_cny_per_1m_tokens
    return round(input_cost + output_cost, 8)


def build_cost_report(records: Sequence[CostRecord]) -> dict[str, Any]:
    """Build a structured cost report from tracked records."""
    provider_costs: dict[str, float] = {}
    provider_calls: dict[str, int] = {}

    for record in records:
        provider_costs[record.provider] = (
            provider_costs.get(record.provider, 0.0) + record.cost_cny
        )
        provider_calls[record.provider] = (
            provider_calls.get(record.provider, 0) + 1
        )

    return {
        "call_count": len(records),
        "estimated_call_count": sum(
            1 for record in records if record.estimated
        ),
        "prompt_tokens": sum(record.prompt_tokens for record in records),
        "completion_tokens": sum(
            record.completion_tokens for record in records
        ),
        "total_tokens": sum(record.total_tokens for record in records),
        "prompt_cache_hit_tokens": sum(
            record.prompt_cache_hit_tokens for record in records
        ),
        "prompt_cache_miss_tokens": sum(
            record.prompt_cache_miss_tokens for record in records
        ),
        "estimated_cost_cny": round(
            sum(record.cost_cny for record in records),
            8,
        ),
        "provider_calls": dict(sorted(provider_calls.items())),
        "provider_costs_cny": {
            provider: round(cost, 8)
            for provider, cost in sorted(provider_costs.items())
        },
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


def chat(
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> tuple[str, Usage]:
    """Send a prompt and return text plus usage.

    Args:
        prompt: User prompt.
        system_prompt: Optional system prompt.
        temperature: Sampling temperature.
        max_tokens: Optional maximum completion tokens.

    Returns:
        A tuple of response text and normalized usage.
    """
    response = quick_chat(
        prompt=prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.content, response.usage


def chat_json(
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> tuple[dict[str, Any], Usage]:
    """Send a prompt and parse the response as a JSON object.

    Args:
        prompt: User prompt.
        system_prompt: Optional system prompt.
        temperature: Sampling temperature.
        max_tokens: Optional maximum completion tokens.

    Returns:
        A tuple of parsed JSON object and normalized usage.

    Raises:
        LLMClientError: If the model does not return a JSON object.
    """
    text, usage = chat(
        prompt=prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    try:
        payload = json.loads(extract_json_object(text))
    except json.JSONDecodeError as exc:
        raise LLMClientError("LLM response is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise LLMClientError("LLM JSON response must be an object.")

    return payload, usage


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
        prompt_tokens = coerce_non_negative_int(
            usage_payload.get("prompt_tokens")
            or usage_payload.get("input_tokens")
        )
        completion_tokens = coerce_non_negative_int(
            usage_payload.get("completion_tokens")
            or usage_payload.get("output_tokens")
        )
        total_tokens = coerce_non_negative_int(
            usage_payload.get("total_tokens")
            or prompt_tokens + completion_tokens
        )
        prompt_cache_hit_tokens, prompt_cache_miss_tokens = (
            extract_prompt_cache_tokens(usage_payload, prompt_tokens)
        )
        estimated = False
    else:
        prompt_tokens = estimate_tokens_for_messages(messages)
        completion_tokens = estimate_tokens(content)
        total_tokens = prompt_tokens + completion_tokens
        prompt_cache_hit_tokens = 0
        prompt_cache_miss_tokens = prompt_tokens
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
        prompt_cache_hit_tokens=prompt_cache_hit_tokens,
        prompt_cache_miss_tokens=prompt_cache_miss_tokens,
    )


def extract_prompt_cache_tokens(
    usage_payload: dict[str, Any],
    prompt_tokens: int,
) -> tuple[int, int]:
    """Extract cached and uncached prompt token counts from usage metadata."""
    cache_hit_tokens = coerce_non_negative_int(
        usage_payload.get("prompt_cache_hit_tokens")
    )
    cache_miss_tokens = coerce_non_negative_int(
        usage_payload.get("prompt_cache_miss_tokens")
    )

    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        if cache_hit_tokens > 0:
            break
        details = usage_payload.get(details_key)
        if isinstance(details, dict):
            cache_hit_tokens = coerce_non_negative_int(
                details.get("cached_tokens")
            )

    if cache_hit_tokens > 0 and cache_miss_tokens == 0:
        cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)
    elif cache_hit_tokens == 0 and cache_miss_tokens == 0:
        cache_miss_tokens = prompt_tokens

    return cache_hit_tokens, cache_miss_tokens


def coerce_non_negative_int(value: Any) -> int:
    """Convert a provider usage value to a non-negative integer."""
    try:
        coerced_value = int(value)
    except (TypeError, ValueError):
        return 0

    return max(0, coerced_value)


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
    tracker.report(response.provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
