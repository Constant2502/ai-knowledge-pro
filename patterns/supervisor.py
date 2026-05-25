"""Supervisor pattern for iterative agent quality review."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.model_client import chat  # noqa: E402


LOGGER = logging.getLogger(__name__)

PASSING_SCORE = 7
DEFAULT_WORKER_MAX_TOKENS = 1200
DEFAULT_SUPERVISOR_MAX_TOKENS = 500


def supervisor(task: str, max_retries: int = 3) -> dict:
    """Run a worker agent and supervise its output quality.

    Args:
        task: Task description to be analyzed by the worker.
        max_retries: Maximum worker attempts, including the first attempt.

    Returns:
        A result dictionary containing output, attempts, final_score, and an
        optional warning when the result is forced after failed reviews.

    Raises:
        ValueError: If task is empty or max_retries is less than one.
    """
    normalized_task = task.strip()
    if not normalized_task:
        raise ValueError("task must not be empty.")
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1.")

    feedback: str | None = None
    final_output: dict[str, Any] | str = {}
    final_score = 0

    for attempt in range(1, max_retries + 1):
        worker_text = run_worker(
            task=normalized_task,
            feedback=feedback,
            attempt=attempt,
        )
        parsed_output, parse_error = parse_json_object(worker_text)
        final_output = parsed_output if parsed_output is not None else worker_text

        review = run_supervisor_review(
            task=normalized_task,
            worker_output=worker_text,
            parse_error=parse_error,
        )
        if parse_error:
            review = downgrade_invalid_worker_output(review, parse_error)

        final_score = review["score"]
        feedback = review["feedback"]

        if final_score >= PASSING_SCORE:
            return {
                "output": final_output,
                "attempts": attempt,
                "final_score": final_score,
            }

    return {
        "output": final_output,
        "attempts": max_retries,
        "final_score": final_score,
        "warning": (
            f"Supervisor review did not pass after {max_retries} attempts; "
            "returning the latest worker output."
        ),
    }


def run_worker(task: str, feedback: str | None, attempt: int) -> str:
    """Ask the worker agent to produce a JSON analysis report."""
    system_prompt = (
        "你是 AI 知识库项目中的 Worker Agent。"
        "你必须只返回一个可解析的 JSON 对象，不要使用 Markdown 代码块，"
        "不要返回解释性前后缀。"
    )
    prompt_payload: dict[str, Any] = {
        "task": task,
        "attempt": attempt,
        "role": "worker",
        "required_output": {
            "summary": "对任务的一句话结论",
            "analysis": "结构化分析正文",
            "key_points": ["关键点"],
            "risks": ["风险或不确定性"],
            "confidence": "high|medium|low",
        },
        "constraints": [
            "输出必须是 JSON object。",
            "不要编造来源、事实或时间；不确定时写入 risks。",
            "summary 不能只复述任务。",
        ],
    }
    if feedback:
        prompt_payload["supervisor_feedback"] = feedback
        prompt_payload["revision_instruction"] = (
            "根据 supervisor_feedback 修正上一轮问题后重新输出完整 JSON。"
        )

    text, _usage = chat(
        prompt=json.dumps(prompt_payload, ensure_ascii=False),
        system_prompt=system_prompt,
        temperature=0.2,
        max_tokens=DEFAULT_WORKER_MAX_TOKENS,
    )
    return text.strip()


def run_supervisor_review(
    task: str,
    worker_output: str,
    parse_error: str | None,
) -> dict[str, Any]:
    """Ask the supervisor agent to score worker output quality."""
    system_prompt = (
        "你是严格的 Supervisor Agent，负责审核 Worker Agent 输出质量。"
        "你必须只返回 JSON 对象，格式固定为："
        '{"passed": bool, "score": int, "feedback": str}。'
    )
    prompt_payload = {
        "task": task,
        "worker_output": worker_output,
        "worker_json_parse_error": parse_error or "",
        "scoring_dimensions": {
            "accuracy": "准确性，1-10 分",
            "depth": "深度，1-10 分",
            "format": "格式，1-10 分",
        },
        "rules": [
            "score 是三个维度的综合分，必须是 1 到 10 的整数。",
            f"score >= {PASSING_SCORE} 时 passed 为 true，否则为 false。",
            "feedback 必须指出主要问题，并给出下一轮可执行修改建议。",
            "如果 worker_output 不是可解析 JSON，format 必须低分。",
        ],
        "required_output": {"passed": "bool", "score": "int", "feedback": "str"},
    }

    text, _usage = chat(
        prompt=json.dumps(prompt_payload, ensure_ascii=False),
        system_prompt=system_prompt,
        temperature=0.0,
        max_tokens=DEFAULT_SUPERVISOR_MAX_TOKENS,
    )
    parsed_review, parse_error_text = parse_json_object(text)
    if parsed_review is None:
        return {
            "passed": False,
            "score": 0,
            "feedback": (
                "Supervisor returned invalid JSON review: "
                f"{parse_error_text or 'unknown parse error'}"
            ),
        }

    return normalize_review(parsed_review)


def parse_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a JSON object from plain text or a Markdown JSON code fence."""
    candidate = extract_json_object(text)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg}"

    if not isinstance(payload, dict):
        return None, "JSON value must be an object"

    return payload, None


def extract_json_object(text: str) -> str:
    """Extract a likely JSON object from a model response."""
    stripped_text = text.strip()
    if stripped_text.startswith("{") and stripped_text.endswith("}"):
        return stripped_text

    fenced_match = re.search(
        r"```(?:json)?\s*(?P<json>\{.*?\})\s*```",
        stripped_text,
        re.IGNORECASE | re.DOTALL,
    )
    if fenced_match:
        return fenced_match.group("json")

    start = stripped_text.find("{")
    end = stripped_text.rfind("}")
    if start >= 0 and end > start:
        return stripped_text[start : end + 1]

    return stripped_text


def normalize_review(review: dict[str, Any]) -> dict[str, Any]:
    """Normalize supervisor review JSON to the public schema."""
    score = normalize_score(review.get("score"))
    feedback = review.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        feedback = "Supervisor did not provide actionable feedback."

    return {
        "passed": score >= PASSING_SCORE,
        "score": score,
        "feedback": feedback.strip(),
    }


def normalize_score(value: Any) -> int:
    """Normalize a model-provided score to an integer from 0 to 10."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        score = value
    elif isinstance(value, float):
        score = round(value)
    elif isinstance(value, str):
        try:
            score = round(float(value.strip()))
        except ValueError:
            return 0
    else:
        return 0

    return max(0, min(10, score))


def downgrade_invalid_worker_output(
    review: dict[str, Any],
    parse_error: str,
) -> dict[str, Any]:
    """Force a failed review when the worker output is not valid JSON."""
    feedback = (
        f"Worker output is not valid JSON ({parse_error}). "
        f"{review.get('feedback', '')}"
    ).strip()
    return {
        "passed": False,
        "score": min(normalize_score(review.get("score")), 4),
        "feedback": feedback,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the manual supervisor test."""
    parser = argparse.ArgumentParser(description="Run supervisor pattern test.")
    parser.add_argument("task", nargs="*", help="Task text for the worker.")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum worker attempts.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a manual supervisor pattern test."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    task = " ".join(args.task).strip()
    if not task:
        LOGGER.error("Task is required.")
        return 1

    try:
        result = supervisor(task=task, max_retries=args.max_retries)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    LOGGER.info("%s", json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
