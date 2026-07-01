#!/usr/bin/env python3
"""MCP stdio server exposing local OpenAI-compatible chat models."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


DEFAULT_BASE_URL = ""  # no baked-in default; the endpoint is private — set LOCAL_BASE_URL in .env
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MODELS_PATH = "/models"
DEFAULT_CHAT_COMPLETIONS_PATH = "/chat/completions"
# Generous default budgets: local reasoning models (e.g. Qwen3.x) spend a large,
# unpredictable share of max_tokens on hidden reasoning, so a low cap truncates the
# visible answer mid-section. Local models are unlimited, so we err on the high side.
DEFAULT_MAX_TOKENS = 8192
DEFAULT_COMPRESS_MAX_TOKENS = 4096
MAX_ATTEMPTS = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}


logger = logging.getLogger("local_llm_mcp")
logger.setLevel(logging.INFO)
logger.propagate = False
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(handler)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_event(event: str, **fields: Any) -> None:
    record = {"event": event, **fields}
    logger.info(json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")))


def load_env_file() -> None:
    """Load KEY=VALUE pairs from a .env next to this script into os.environ.

    Dependency-free. Existing environment variables always win, so values passed
    by Claude Code via `--env` (or an exported shell) override the file. This lets
    `python server.py --list-models` and a path-only MCP registration both work
    without re-declaring the config in two places.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class ConfigError(RuntimeError):
    """Configuration or startup error."""


class LocalAPIError(RuntimeError):
    """Actionable error from the local model endpoint."""


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    max_concurrency: int
    timeout_seconds: float
    models_path: str
    chat_completions_path: str

    @classmethod
    def from_env(cls) -> "Config":
        base_url = os.environ.get("LOCAL_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        if not base_url:
            raise ConfigError("LOCAL_BASE_URL is empty. Set it to the OpenAI-compatible API base URL.")

        api_key = os.environ.get("LOCAL_API_KEY", "").strip()
        models_path = normalize_api_path(os.environ.get("LOCAL_MODELS_PATH", DEFAULT_MODELS_PATH), "LOCAL_MODELS_PATH")
        chat_completions_path = normalize_api_path(
            os.environ.get("LOCAL_CHAT_COMPLETIONS_PATH", DEFAULT_CHAT_COMPLETIONS_PATH),
            "LOCAL_CHAT_COMPLETIONS_PATH",
        )
        max_concurrency = parse_positive_int(
            "LOCAL_MAX_CONCURRENCY",
            os.environ.get("LOCAL_MAX_CONCURRENCY"),
            DEFAULT_MAX_CONCURRENCY,
        )
        timeout_seconds = parse_positive_float(
            "LOCAL_TIMEOUT_SECONDS",
            os.environ.get("LOCAL_TIMEOUT_SECONDS"),
            DEFAULT_TIMEOUT_SECONDS,
        )

        return cls(
            base_url=base_url,
            api_key=api_key,
            max_concurrency=max_concurrency,
            timeout_seconds=timeout_seconds,
            models_path=models_path,
            chat_completions_path=chat_completions_path,
        )


@dataclass
class ChatCompletion:
    content: str
    latency_ms: int
    usage: dict[str, Any] | None
    finish_reason: str | None = None


def parse_positive_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer; got {raw!r}.") from exc
    if value < 1:
        raise ConfigError(f"{name} must be a positive integer; got {raw!r}.")
    return value


def parse_positive_float(name: str, raw: str | None, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive number of seconds; got {raw!r}.") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive number of seconds; got {raw!r}.")
    return value


def normalize_api_path(raw: str, name: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ConfigError(f"{name} must not be empty.")
    if not value.startswith("/"):
        value = f"/{value}"
    return value.rstrip("/") or "/"


def elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def preview_body(text: str, limit: int = 1000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def usage_token_count(usage: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    return value if isinstance(value, int) else None


def empty_content_diagnostic(
    model: str,
    *,
    finish_reason: str | None,
    usage: dict[str, Any] | None,
    max_tokens: int,
) -> dict[str, Any]:
    """Build an actionable error for a completion that returned empty content.

    Distinguishes the common reasoning-model case (hidden chain-of-thought ate the
    whole max_tokens budget, finish_reason="length") from a generic empty response.
    """
    completion_tokens = usage_token_count(usage, "completion_tokens")
    reasoning_tokens = usage_token_count(usage, "reasoning_tokens")
    budget_exhausted = finish_reason == "length" or (
        completion_tokens is not None and completion_tokens >= max_tokens
    )
    if budget_exhausted:
        error = (
            f"Model {model!r} returned empty content because the max_tokens budget "
            f"({max_tokens}) was consumed by hidden reasoning "
            f"(reasoning_tokens={reasoning_tokens}, completion_tokens={completion_tokens}, "
            f"finish_reason={finish_reason!r}). Raise max_tokens (e.g. >=4000) and retry."
        )
    else:
        error = (
            f"Model {model!r} returned empty content "
            f"(finish_reason={finish_reason!r}, completion_tokens={completion_tokens})."
        )
    return {
        "error": error,
        "finish_reason": finish_reason,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "max_tokens": max_tokens,
    }


class LocalLLMService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._models_by_id: dict[str, dict[str, Any]] = {}
        self._model_ids: list[str] = []
        self._discovery_lock = asyncio.Lock()

    @property
    def model_ids(self) -> list[str]:
        return list(self._model_ids)

    @property
    def models_by_id(self) -> dict[str, dict[str, Any]]:
        return dict(self._models_by_id)

    def _headers(self, *, include_content_type: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if include_content_type:
            headers["Content-Type"] = "application/json"
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def ensure_discovered(self) -> None:
        if self._model_ids:
            return
        async with self._discovery_lock:
            if not self._model_ids:
                await self.discover_models()

    async def discover_models(self) -> None:
        url = f"{self.config.base_url}{self.config.models_path}"
        start = time.perf_counter()
        last_error: str | None = None
        response: httpx.Response | None = None

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = await client.get(url, headers=self._headers())
                except httpx.HTTPError as exc:
                    last_error = f"{exc.__class__.__name__}: {exc}"
                    if attempt == MAX_ATTEMPTS:
                        raise LocalAPIError(
                            "Failed to discover local models from "
                            f"{url}: {last_error}. "
                            "Set LOCAL_BASE_URL to the Open WebUI/OpenAI-compatible API root and LOCAL_MODELS_PATH if needed. "
                            "If the endpoint requires auth, set LOCAL_API_KEY so this server sends "
                            "Authorization: Bearer <key>."
                        ) from exc
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                    continue

                if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                    last_error = f"HTTP {response.status_code}: {preview_body(response.text)}"
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                    continue

                break

        latency = elapsed_ms(start)
        if response is None:
            raise LocalAPIError(
                "Failed to discover local models before receiving any HTTP response. "
                "Set LOCAL_BASE_URL to the Open WebUI/OpenAI-compatible API root and LOCAL_MODELS_PATH if needed. "
                "If the endpoint requires auth, set LOCAL_API_KEY so this server sends "
                f"Authorization: Bearer <key>. Last error: {last_error}"
            )

        if response.status_code != 200:
            auth_detail = (
                "LOCAL_API_KEY is set, so an Authorization: Bearer <key> header was sent."
                if self.config.api_key
                else "LOCAL_API_KEY is empty, so no Authorization header was sent."
            )
            raise LocalAPIError(
                "Failed to discover local models from "
                f"{url}: HTTP {response.status_code}. "
                "Set LOCAL_BASE_URL to the Open WebUI/OpenAI-compatible API root and LOCAL_MODELS_PATH if needed. "
                "If the endpoint requires auth, set LOCAL_API_KEY so this server sends "
                f"Authorization: Bearer <key>. {auth_detail} "
                f"Response body: {preview_body(response.text)}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise LocalAPIError(
                f"Failed to parse JSON from {url}. The endpoint returned HTTP {response.status_code} "
                "with non-JSON content. Ensure LOCAL_BASE_URL points at the OpenAI-compatible "
                "API root so $LOCAL_BASE_URL$LOCAL_MODELS_PATH returns JSON, not a browser UI. "
                "If authentication changes the response, set LOCAL_API_KEY so this server sends "
                f"Authorization: Bearer <key>. Response body: {preview_body(response.text)}"
            ) from exc

        model_ids, models_by_id = self._parse_models_payload(payload)
        self._model_ids = model_ids
        self._models_by_id = models_by_id
        log_event(
            "models_discovered",
            base_url=self.config.base_url,
            models_path=self.config.models_path,
            count=len(model_ids),
            model_ids=model_ids,
            latency_ms=latency,
        )

    def _parse_models_payload(self, payload: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
        raw_models = self._extract_model_items(payload)
        models_by_id: dict[str, dict[str, Any]] = {}

        for item in raw_models:
            model_id, metadata = self._extract_model_id(item)
            if model_id:
                models_by_id[model_id] = metadata

        model_ids = sorted(models_by_id)
        if not model_ids:
            raise LocalAPIError(
                "The model discovery response contained no usable model IDs. Expected OpenAI "
                'items with an "id" field or Open WebUI items with "id"/"name"/"model".'
            )
        return model_ids, models_by_id

    def _extract_model_items(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise LocalAPIError(
                "The model discovery response was not a JSON object/list. "
                f"Received: {preview_body(json.dumps(payload, ensure_ascii=False, default=str))}"
            )

        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested_items = [
                    {"id": nested_key, **nested_value}
                    if isinstance(nested_value, dict) else {"id": nested_key, "value": nested_value}
                    for nested_key, nested_value in value.items()
                ]
                if nested_items:
                    return nested_items

        if all(isinstance(value, dict) for value in payload.values()):
            return [
                {"id": key, **value}
                for key, value in payload.items()
                if isinstance(value, dict)
            ]

        raise LocalAPIError(
            "The model discovery response did not look like OpenAI or Open WebUI model JSON. "
            'Expected a list, {"data": [...]}, or {"models": [...]}. '
            f"Received: {preview_body(json.dumps(payload, ensure_ascii=False, default=str))}"
        )

    def _extract_model_id(self, item: Any) -> tuple[str | None, dict[str, Any]]:
        if isinstance(item, str):
            return item, {"id": item}
        if not isinstance(item, dict):
            return None, {}

        for key in ("id", "name", "model"):
            raw_id = item.get(key)
            if isinstance(raw_id, str) and raw_id.strip():
                return raw_id, item

        nested_model = item.get("model")
        if isinstance(nested_model, dict):
            for key in ("id", "name"):
                raw_id = nested_model.get(key)
                if isinstance(raw_id, str) and raw_id.strip():
                    return raw_id, item

        return None, item

    def invalid_model_error(self, model: str) -> str:
        valid = ", ".join(self._model_ids) if self._model_ids else "<none discovered>"
        return f"Unknown model {model!r}. Valid discovered model IDs: {valid}"

    async def validate_model(self, model: str) -> str | None:
        await self.ensure_discovered()
        if model not in self._models_by_id:
            return self.invalid_model_error(model)
        return None

    async def chat_completion(
        self,
        *,
        model: str,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ChatCompletion:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response_payload = await self._request_json_with_retries(
                client,
                "POST",
                self.config.chat_completions_path,
                json_payload=payload,
            )

        choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
        if not isinstance(choices, list) or not choices:
            raise LocalAPIError(
                f"Chat completion for model {model!r} returned no choices. "
                f"Response: {preview_body(json.dumps(response_payload, ensure_ascii=False, default=str))}"
            )

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise LocalAPIError(f"Chat completion for model {model!r} returned an invalid choice.")

        message = first_choice.get("message")
        if isinstance(message, dict):
            content = normalize_content(message.get("content"))
        else:
            content = normalize_content(first_choice.get("text"))

        finish_reason = first_choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            finish_reason = None

        usage = response_payload.get("usage") if isinstance(response_payload, dict) else None
        return ChatCompletion(
            content=content,
            latency_ms=elapsed_ms(start),
            usage=usage,
            finish_reason=finish_reason,
        )

    async def _request_json_with_retries(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        last_error: str | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers(include_content_type=json_payload is not None),
                    json=json_payload,
                )
            except httpx.HTTPError as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
                if attempt == MAX_ATTEMPTS:
                    break
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                continue

            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                last_error = f"HTTP {response.status_code}: {preview_body(response.text)}"
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                continue

            if response.status_code < 200 or response.status_code >= 300:
                raise LocalAPIError(
                    f"{method} {url} failed with HTTP {response.status_code}. "
                    f"Response body: {preview_body(response.text)}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise LocalAPIError(
                    f"{method} {url} returned a non-JSON response. Ensure LOCAL_BASE_URL and "
                    "LOCAL_CHAT_COMPLETIONS_PATH point at the API endpoint and set LOCAL_API_KEY if auth is required. "
                    f"Response body: {preview_body(response.text)}"
                ) from exc

            if not isinstance(payload, dict):
                raise LocalAPIError(
                    f"{method} {url} returned JSON that was not an object: "
                    f"{preview_body(json.dumps(payload, ensure_ascii=False, default=str))}"
                )
            return payload

        raise LocalAPIError(
            f"{method} {url} failed after {MAX_ATTEMPTS} attempts due to a retryable error. "
            "Check LOCAL_BASE_URL, LOCAL_CHAT_COMPLETIONS_PATH, network reachability, and LOCAL_API_KEY if auth is required. "
            f"Last error: {last_error}"
        )


mcp = FastMCP("local-llm-orchestrator")
SERVICE: LocalLLMService | None = None


def get_service() -> LocalLLMService:
    global SERVICE
    if SERVICE is None:
        SERVICE = LocalLLMService(Config.from_env())
    return SERVICE


@mcp.tool()
async def list_local_models() -> dict[str, Any]:
    """Return model IDs and metadata discovered from the local OpenAI-compatible endpoint."""
    start = time.perf_counter()
    error: str | None = None
    try:
        service = get_service()
        await service.ensure_discovered()
        return {
            "base_url": service.config.base_url,
            "models_path": service.config.models_path,
            "chat_completions_path": service.config.chat_completions_path,
            "count": len(service.model_ids),
            "models": service.model_ids,
            "metadata": service.models_by_id,
        }
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        log_event(
            "tool_call",
            tool="list_local_models",
            model=None,
            prompt_length=0,
            latency_ms=elapsed_ms(start),
            usage=None,
            error=error,
        )


@mcp.tool()
async def local_generate(
    model: str,
    prompt: str,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Generate one chat completion using a discovered local model."""
    start = time.perf_counter()
    usage: dict[str, Any] | None = None
    error: str | None = None
    try:
        service = get_service()
        validation_error = await service.validate_model(model)
        if validation_error:
            error = validation_error
            return {"ok": False, "model": model, "error": validation_error, "valid_models": service.model_ids}

        completion = await service.chat_completion(
            model=model,
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = completion.usage
        if not completion.content.strip():
            diagnostic = empty_content_diagnostic(
                model,
                finish_reason=completion.finish_reason,
                usage=completion.usage,
                max_tokens=max_tokens,
            )
            error = diagnostic["error"]
            return {"ok": False, "model": model, "latency_ms": completion.latency_ms, **diagnostic}
        return {
            "ok": True,
            "model": model,
            "content": completion.content,
            "latency_ms": completion.latency_ms,
            "usage": completion.usage,
            "finish_reason": completion.finish_reason,
        }
    except Exception as exc:
        error = str(exc)
        return {"ok": False, "model": model, "error": error}
    finally:
        log_event(
            "tool_call",
            tool="local_generate",
            model=model,
            prompt_length=len(prompt),
            latency_ms=elapsed_ms(start),
            usage=usage,
            error=error,
        )


@mcp.tool()
async def local_batch(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run many local chat completions concurrently and return results in input order."""
    start = time.perf_counter()
    service = get_service()
    await service.ensure_discovered()
    semaphore = asyncio.Semaphore(service.config.max_concurrency)

    async def run_job(index: int, job: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await run_batch_job(index, job)

    results = await asyncio.gather(*(run_job(index, job) for index, job in enumerate(jobs)))
    error_count = sum(1 for result in results if not result.get("ok"))
    log_event(
        "tool_call",
        tool="local_batch",
        model=None,
        prompt_length=sum(len(str(job.get("prompt", ""))) if isinstance(job, dict) else 0 for job in jobs),
        latency_ms=elapsed_ms(start),
        usage=None,
        error=f"{error_count} failed job(s)" if error_count else None,
        job_count=len(jobs),
        concurrency=service.config.max_concurrency,
    )
    return results


async def run_batch_job(index: int, job: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    model = "<missing>"
    prompt = ""
    usage: dict[str, Any] | None = None
    error: str | None = None

    try:
        if not isinstance(job, dict):
            raise ValueError("Each job must be an object with model, prompt, system?, temperature?, max_tokens?.")

        model = require_string(job, "model")
        prompt = require_string(job, "prompt")
        system = optional_string(job, "system", "")
        temperature = optional_float(job, "temperature", 0.7)
        max_tokens = optional_positive_int(job, "max_tokens", DEFAULT_MAX_TOKENS)

        service = get_service()
        validation_error = await service.validate_model(model)
        if validation_error:
            raise ValueError(validation_error)

        completion = await service.chat_completion(
            model=model,
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = completion.usage
        if not completion.content.strip():
            diagnostic = empty_content_diagnostic(
                model,
                finish_reason=completion.finish_reason,
                usage=completion.usage,
                max_tokens=max_tokens,
            )
            error = diagnostic["error"]
            return {
                "index": index,
                "model": model,
                "ok": False,
                "latency_ms": completion.latency_ms,
                **diagnostic,
            }
        return {
            "index": index,
            "model": model,
            "ok": True,
            "content": completion.content,
            "latency_ms": completion.latency_ms,
            "usage": completion.usage,
            "finish_reason": completion.finish_reason,
        }
    except Exception as exc:
        error = str(exc)
        return {
            "index": index,
            "model": model,
            "ok": False,
            "error": error,
            "latency_ms": elapsed_ms(start),
        }
    finally:
        log_event(
            "tool_call",
            tool="local_batch",
            model=model,
            prompt_length=len(prompt),
            latency_ms=elapsed_ms(start),
            usage=usage,
            error=error,
            job_index=index,
        )


@mcp.tool()
async def local_compress(
    text: str,
    model: str,
    instruction: str = "Summarize and structure the key facts, preserving all decision-relevant detail.",
    max_tokens: int = DEFAULT_COMPRESS_MAX_TOKENS,
) -> str:
    """Compress large text through a local model before sending it to the orchestrator."""
    start = time.perf_counter()
    usage: dict[str, Any] | None = None
    error: str | None = None
    try:
        service = get_service()
        validation_error = await service.validate_model(model)
        if validation_error:
            raise ValueError(validation_error)

        completion = await service.chat_completion(
            model=model,
            prompt=text,
            system=instruction,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        usage = completion.usage
        if not completion.content.strip():
            diagnostic = empty_content_diagnostic(
                model,
                finish_reason=completion.finish_reason,
                usage=completion.usage,
                max_tokens=max_tokens,
            )
            raise LocalAPIError(diagnostic["error"])
        return completion.content
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        log_event(
            "tool_call",
            tool="local_compress",
            model=model,
            prompt_length=len(text),
            latency_ms=elapsed_ms(start),
            usage=usage,
            error=error,
        )


def require_string(job: dict[str, Any], key: str) -> str:
    value = job.get(key)
    if not isinstance(value, str) or value == "":
        raise ValueError(f"Job field {key!r} must be a non-empty string.")
    return value


def optional_string(job: dict[str, Any], key: str, default: str) -> str:
    value = job.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"Job field {key!r} must be a string when provided.")
    return value


def optional_float(job: dict[str, Any], key: str, default: float) -> float:
    value = job.get(key, default)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Job field {key!r} must be numeric when provided.") from exc
    if number < 0:
        raise ValueError(f"Job field {key!r} must be non-negative.")
    return number


def optional_positive_int(job: dict[str, Any], key: str, default: int) -> int:
    value = job.get(key, default)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Job field {key!r} must be a positive integer when provided.") from exc
    if number < 1:
        raise ValueError(f"Job field {key!r} must be a positive integer.")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCP stdio server for local OpenAI-compatible LLMs.")
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="Discover available models, print them as JSON, and exit.",
    )
    return parser.parse_args()


async def print_models_and_exit() -> None:
    service = get_service()
    await service.discover_models()
    print(
        json.dumps(
            {
                "base_url": service.config.base_url,
                "models_path": service.config.models_path,
                "chat_completions_path": service.config.chat_completions_path,
                "count": len(service.model_ids),
                "models": service.model_ids,
                "metadata": service.models_by_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> int:
    load_env_file()
    args = parse_args()
    try:
        if args.list_models:
            asyncio.run(print_models_and_exit())
            return 0

        service = get_service()
        asyncio.run(service.discover_models())
        mcp.run()
        return 0
    except (ConfigError, LocalAPIError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
