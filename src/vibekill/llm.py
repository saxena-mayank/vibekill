"""Local model client.

Deliberately small: about two HTTP calls and a JSON extractor. LangGraph handles
orchestration, so this layer does not need a framework of its own, and a
contributor can read it in one sitting.

Three providers:

* ``ollama``  - native ``/api/chat``
* ``openai``  - any OpenAI-compatible ``/v1/chat/completions``: llama.cpp server,
  vLLM, LM Studio, TGI, or a self-hosted gateway
* ``none``    - no model at all; agents fall back to reporting deterministic
  evidence only

``auto`` probes the first two and settles on ``none`` if neither answers, so a
review never fails just because the model host is down.

Concurrency is capped by a semaphore. One local runtime serving four agents does
not get faster past two in-flight requests: the weights have to cross the memory
bus for every token regardless of how many callers are waiting.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from .settings import LLMSettings

logger = logging.getLogger("vibekill.llm")


class LLMError(RuntimeError):
    """Model call failed. Agents catch this and degrade rather than abort."""


@dataclass
class LLMUsage:
    calls: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0
    failures: int = 0

    def record(self, prompt: str, completion: str) -> None:
        self.calls += 1
        self.prompt_chars += len(prompt)
        self.completion_chars += len(completion)


@dataclass
class LLMClient:
    """Thread-safe chat client for a single local model."""

    settings: LLMSettings
    provider: str = "none"
    usage: LLMUsage = field(default_factory=LLMUsage)
    _semaphore: threading.Semaphore = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._semaphore = threading.Semaphore(self.settings.max_concurrency)

    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        return self.provider != "none"

    @property
    def model(self) -> str:
        return self.settings.model if self.available else "none"

    def describe(self) -> str:
        if not self.available:
            return "no model (deterministic analysis only)"
        return f"{self.provider}:{self.settings.model} at {self.settings.base_url}"

    # ------------------------------------------------------------------ #

    def chat(self, system: str, user: str, *, json_mode: bool = True) -> str:
        """One chat completion. Returns raw text; raises `LLMError` on failure."""
        if not self.available:
            raise LLMError("no model configured")

        import httpx

        with self._semaphore:
            attempts = 2
            last_error: Exception | None = None
            for attempt in range(attempts):
                try:
                    if self.provider == "ollama":
                        text = self._chat_ollama(system, user, json_mode)
                    else:
                        text = self._chat_openai(system, user, json_mode)
                    with self._lock:
                        self.usage.record(system + user, text)
                    return text
                except (httpx.HTTPError, LLMError, KeyError, ValueError) as exc:
                    last_error = exc
                    logger.debug("model call failed (attempt %d/%d): %s", attempt + 1, attempts, exc)
            with self._lock:
                self.usage.failures += 1
            raise LLMError(f"model call failed: {last_error}") from last_error

    def _chat_ollama(self, system: str, user: str, json_mode: bool) -> str:
        import httpx

        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.settings.temperature,
                "num_predict": self.settings.max_output_tokens,
            },
        }
        if self.settings.seed is not None:
            payload["options"]["seed"] = self.settings.seed
        if json_mode:
            payload["format"] = "json"

        with httpx.Client(timeout=self.settings.timeout_s) as client:
            response = client.post(f"{self.settings.base_url.rstrip('/')}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        return (data.get("message") or {}).get("content", "") or ""

    def _chat_openai(self, system: str, user: str, json_mode: bool) -> str:
        import httpx

        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_output_tokens,
            "stream": False,
        }
        if self.settings.seed is not None:
            payload["seed"] = self.settings.seed
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"

        url = f"{self.settings.base_url.rstrip('/')}/v1/chat/completions"
        with httpx.Client(timeout=self.settings.timeout_s) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("response contained no choices")
        return (choices[0].get("message") or {}).get("content", "") or ""


# --------------------------------------------------------------------------- #
# Construction and probing
# --------------------------------------------------------------------------- #


def build_client(settings: LLMSettings, *, probe: bool = True) -> LLMClient:
    """Resolve `provider="auto"` into a concrete provider.

    Probing is a short request to each candidate. If nothing answers, the client
    reports itself unavailable and the review proceeds on deterministic analysis
    alone rather than failing.
    """
    if settings.provider != "auto":
        client = LLMClient(settings=settings, provider=settings.provider)
        if settings.provider != "none" and probe and not _reachable(settings, settings.provider):
            logger.warning(
                "configured provider %r at %s is not responding; continuing without a model",
                settings.provider,
                settings.base_url,
            )
            return LLMClient(settings=settings, provider="none")
        return client

    if not probe:
        return LLMClient(settings=settings, provider="none")

    for candidate in ("ollama", "openai"):
        if _reachable(settings, candidate):
            logger.info("using %s provider at %s", candidate, settings.base_url)
            return LLMClient(settings=settings, provider=candidate)

    logger.info("no local model reachable at %s; deterministic analysis only", settings.base_url)
    return LLMClient(settings=settings, provider="none")


def _reachable(settings: LLMSettings, provider: str) -> bool:
    """Cheap liveness check against a model host."""
    import httpx

    base = settings.base_url.rstrip("/")
    url = f"{base}/api/tags" if provider == "ollama" else f"{base}/v1/models"
    try:
        with httpx.Client(timeout=5.0) as client:
            return client.get(url).status_code < 500
    except Exception:
        # Includes EgressBlocked when the air-gap guard refuses the host, which
        # is a legitimate "not reachable" answer rather than an error.
        return False


# --------------------------------------------------------------------------- #
# Output parsing
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model response.

    Small models wrap JSON in prose, in code fences, or emit a trailing comma.
    Rather than demanding perfect output, recover what we can: a dropped finding
    is invisible to the user, so leniency here directly improves recall.
    """
    if not text or not text.strip():
        raise ValueError("empty response")

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)
    candidates.extend(match.strip() for match in _FENCE_RE.findall(text))

    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        for attempt in (candidate, _repair_json(candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no parsable JSON in response: {text[:200]!r}")


def _repair_json(text: str) -> str:
    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    repaired = re.sub(r"(?<=[{,])\s*'([^']+)'\s*:", r' "\1":', repaired)
    return repaired


def coerce_finding_list(payload: Any) -> list[dict[str, Any]]:
    """Normalise the several shapes a model returns into a list of dicts.

    Accepts a bare list, or an object wrapping one under any of the obvious keys.
    Asking for `{"findings": [...]}` and getting `{"issues": [...]}` is routine.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("findings", "issues", "results", "comments", "items", "observations"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # A single finding returned unwrapped.
        if any(k in payload for k in ("title", "message", "description")):
            return [payload]
    return []
