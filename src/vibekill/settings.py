"""Configuration.

One `Settings` object carries every knob. It can be built from defaults, a TOML
file, or `VIBEKILL_*` environment variables, and the same object is used by the CLI,
the FastAPI service and the Streamlit UI so there is exactly one place to look
when behaviour surprises you.

Environment variables use a double-underscore for nesting::

    VIBEKILL_MODE=github
    VIBEKILL_LLM__MODEL=qwen3-coder-next
    VIBEKILL_LLM__BASE_URL=http://127.0.0.1:11434
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import Severity

Mode = Literal["airgapped", "github"]

# Generated, vendored and lock files. A review comment on these is noise.
DEFAULT_EXCLUDES: list[str] = [
    "**/node_modules/**",
    "**/.venv/**",
    "**/dist/**",
    "**/build/**",
    "**/vendor/**",
    "**/__pycache__/**",
    "**/*.min.js",
    "**/*.lock",
    "**/package-lock.json",
    "**/poetry.lock",
    "**/uv.lock",
    "**/go.sum",
    "**/*_pb2.py",
    "**/*.snap",
    "**/migrations/**",
]


class LLMSettings(BaseModel):
    """Local model wiring.

    `provider="auto"` probes Ollama, then an OpenAI-compatible endpoint, and
    falls back to `"none"`. A review with `provider="none"` still runs: the
    deterministic analysers and built-in heuristics carry it. That fallback is
    not a degraded curiosity, it is the configuration many locked-down sites
    will actually deploy.
    """

    provider: Literal["auto", "ollama", "openai", "none"] = "auto"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5-coder:7b"
    # Low temperature and a fixed seed keep CI runs from arguing with each other
    # about the same diff.
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    seed: int | None = 7
    max_output_tokens: int = Field(default=2048, ge=256)
    # Prompt budget in characters (~4 chars per token) so no tokenizer needs to
    # ship inside an air-gapped install.
    context_chars: int = Field(default=32_000, ge=8_000)
    timeout_s: int = Field(default=180, ge=10)
    # One local runtime serves all four agents; past ~2 concurrent requests it
    # thrashes and total wall-clock gets worse rather than better.
    max_concurrency: int = Field(default=2, ge=1, le=16)
    api_key: str | None = None


class ToolSettings(BaseModel):
    """Deterministic analysers. A missing binary is reported, never fatal."""

    enabled: bool = True
    ruff: bool = True
    bandit: bool = True
    semgrep: bool = True
    heuristics: bool = True
    # Local rule directories. `semgrep --config auto` contacts the Semgrep
    # registry, so air-gapped mode refuses it and uses these instead.
    semgrep_configs: list[str] = Field(default_factory=list)
    timeout_s: int = Field(default=120, ge=10)
    # Only surface findings on lines this diff actually touched.
    changed_lines_only: bool = True


class AgentSettings(BaseModel):
    enabled: bool = True
    max_findings: int = Field(default=20, ge=1)
    # Appended to the agent's system prompt: a team's own policy, conventions,
    # or "stop telling us about print statements".
    extra_instructions: str = Field(default="", max_length=2_000)


class AgentsSettings(BaseModel):
    security: AgentSettings = AgentSettings()
    quality: AgentSettings = AgentSettings()
    testing: AgentSettings = AgentSettings()
    complexity: AgentSettings = AgentSettings()

    def get(self, name: str) -> AgentSettings:
        return getattr(self, name)


class GitHubSettings(BaseModel):
    """GitHub PR source configuration."""

    api_url: str = "https://api.github.com"
    token: str | None = None
    repo: str = ""  # owner/name
    pr_number: int | None = None
    timeout_s: int = Field(default=30, ge=5)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIBEKILL_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    mode: Mode = "airgapped"
    # Hard socket-level lockdown. This is what makes "air-gapped" a testable
    # property of the process rather than a deployment promise.
    enforce_airgap: bool = True
    allow_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "::1"])

    diff_context_lines: int = Field(default=5, ge=0)
    max_files: int = Field(default=40, ge=1)
    max_diff_chars: int = Field(default=300_000, ge=1_000)
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))

    # Exit non-zero when a finding at or above this severity survives.
    fail_on: Severity = Severity.BLOCKER
    # Applied to model findings after the evidence check.
    min_confidence: float = Field(default=0.35, ge=0.0, le=1.0)
    # Drop model findings whose quoted snippet does not match the cited line.
    # Turning this off is supported but makes the output markedly less reliable.
    require_evidence: bool = True

    output_dir: Path = Path(".vibekill")

    llm: LLMSettings = LLMSettings()
    tools: ToolSettings = ToolSettings()
    agents: AgentsSettings = AgentsSettings()
    github: GitHubSettings = GitHubSettings()

    @field_validator("fail_on", mode="before")
    @classmethod
    def _parse_severity(cls, value: Any) -> Any:
        return Severity.parse(value, Severity.BLOCKER) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _configure_connected_mode(self) -> Settings:
        """Allow only GitHub and the configured model endpoint in connected mode."""
        if self.mode != "github":
            return self

        if not self.github.token:
            self.github.token = os.getenv("GITHUB_TOKEN")

        for host in (self.api_url_host(), self.llm_host()):
            if host and host not in self.allow_hosts:
                self.allow_hosts.append(host)
        return self

    def api_url_host(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self.github.api_url).hostname or ""

    def llm_host(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self.llm.base_url).hostname or ""

    def llm_host_allowed(self) -> bool:
        """Whether the configured model endpoint survives the air-gap policy."""
        if not self.enforce_airgap:
            return True
        return self.llm_host() in set(self.allow_hosts)

    def only_local_hosts_allowed(self) -> bool:
        """True when every allowlisted network host is loopback."""
        import ipaddress

        aliases = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
        for host in self.allow_hosts:
            if host.lower() in aliases:
                continue
            try:
                if ipaddress.ip_address(host).is_loopback:
                    continue
            except ValueError:
                pass
            return False
        return True

    # ------------------------------------------------------------------ #

    @classmethod
    def load(
        cls,
        config_file: Path | None = None,
        *,
        search_from: Path | None = None,
        **overrides: Any,
    ) -> Settings:
        """Load TOML, then environment variables, then explicit overrides."""
        data: dict[str, Any] = {}
        path = config_file or find_config_file(search_from or Path.cwd())
        if path and path.is_file():
            raw = tomllib.loads(path.read_text("utf-8"))
            data = raw.get("tool", {}).get("vibekill", {}) if path.name == "pyproject.toml" else raw
        merged = _deep_merge(data, _environment_overrides())
        merged = _deep_merge(merged, _prune_none(overrides))
        return cls(**merged)


CONFIG_FILENAMES = ("vibekill.toml", ".vibekill.toml")


def _environment_overrides() -> dict[str, Any]:
    """Read VIBEKILL_* variables without letting file values outrank them."""
    result: dict[str, Any] = {}
    prefix = "VIBEKILL_"
    for name, raw in os.environ.items():
        if not name.startswith(prefix):
            continue
        parts = [part.lower() for part in name[len(prefix) :].split("__")]
        value: Any = raw
        if raw[:1] in "[{" or raw.lower() in {"true", "false", "null"}:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return result


def find_config_file(start: Path) -> Path | None:
    """Walk up from `start` looking for an vibekill config, stopping at the repo root."""
    start = start.resolve()
    for directory in [start, *start.parents]:
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        if (directory / ".git").exists():
            break
    return None


def _prune_none(mapping: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if value is None:
            continue
        out[key] = _prune_none(value) if isinstance(value, dict) else value
    return out


def _deep_merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in top.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
