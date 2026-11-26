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
import subprocess
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


class ScopeSettings(BaseModel):
    """PR comprehension: compare declared intent with the actual change.

    Advisory only. Nothing here changes the verdict or the exit code.
    """

    enabled: bool = True
    # A file is CORE when its link score reaches this. Each kind of evidence (a term
    # shared with the file path, a term shared with a name defined in the change)
    # contributes 0.5, so the default needs one and 1.0 needs both.
    core_min_score: float = Field(default=0.5, ge=0.0, le=1.0)


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
    scope: ScopeSettings = ScopeSettings()
    # Notes produced while loading config, such as policy keys ignored because they
    # came from the PR head. Surfaced in the review's warnings.
    config_warnings: list[str] = Field(default_factory=list)

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
        trusted_ref: str | None = None,
        **overrides: Any,
    ) -> Settings:
        """Load TOML, then environment variables, then explicit overrides.

        `trusted_ref` is a git ref (normally the PR's base branch). When given, the
        policy keys in `POLICY_KEYS` are taken from that ref's copy of the config file
        and the checked-out copy cannot change them, because in CI the checkout is
        the PR head and its author controls it. Environment variables and explicit
        overrides are set by whoever runs the tool, so they still win.
        """
        data: dict[str, Any] = {}
        warnings: list[str] = []
        path = config_file or find_config_file(search_from or Path.cwd())
        if path and path.is_file():
            data = _read_config(path)
            if trusted_ref:
                data, warnings = _apply_trusted_policy(data, path, trusted_ref)
        merged = _deep_merge(data, _environment_overrides())
        merged = _deep_merge(merged, _prune_none(overrides))
        settings = cls(**merged)
        settings.config_warnings = warnings
        return settings


# Keys that decide where data may go. A PR author must not be able to change them.
# Each entry is a path of table names ending in the key.
POLICY_KEYS: tuple[tuple[str, ...], ...] = (
    ("mode",),
    ("enforce_airgap",),
    ("allow_hosts",),
    ("llm", "base_url"),
    ("llm", "api_key"),
    ("github", "api_url"),
    ("github", "token"),
)


def _read_config(path: Path) -> dict[str, Any]:
    raw = tomllib.loads(path.read_text("utf-8"))
    return _vibekill_table(raw, path.name)


def _vibekill_table(raw: dict[str, Any], filename: str) -> dict[str, Any]:
    return raw.get("tool", {}).get("vibekill", {}) if filename == "pyproject.toml" else raw


def _git_show(path: Path, ref: str) -> str | None:
    """Contents of `path` at `ref`, or None when the ref or file does not exist."""
    cwd = path.resolve().parent
    try:
        prefix = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=cwd, capture_output=True, text=True, timeout=30, check=False,
        )
        if prefix.returncode != 0:
            return None
        proc = subprocess.run(
            ["git", "show", f"{ref}:{prefix.stdout.strip()}{path.name}"],
            cwd=cwd, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _get_path(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _set_path(data: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    for key in keys[:-1]:
        data = data.setdefault(key, {})
    if value is None:
        data.pop(keys[-1], None)
    else:
        data[keys[-1]] = value


def _apply_trusted_policy(
    head: dict[str, Any], path: Path, ref: str
) -> tuple[dict[str, Any], list[str]]:
    """Overwrite policy keys in `head` with the values from `ref`.

    Fails closed: if the ref or file cannot be read, the policy keys fall back to
    defaults and a warning says so.
    """
    warnings: list[str] = []
    text = _git_show(path, ref)
    if text is None:
        base: dict[str, Any] = {}
        # A missing file at a valid ref is normal (config added by this PR). An
        # unreadable ref is not, so say which.
        if not _ref_exists(path, ref):
            warnings.append(
                f"could not read {path.name} at {ref!r}; policy keys use built-in defaults"
            )
    else:
        try:
            base = _vibekill_table(tomllib.loads(text), path.name)
        except tomllib.TOMLDecodeError:
            base = {}
            warnings.append(
                f"{path.name} at {ref!r} is not valid TOML; policy keys use built-in defaults"
            )
    merged = json.loads(json.dumps(head))  # deep copy; config values are plain TOML types
    for keys in POLICY_KEYS:
        head_value, base_value = _get_path(head, keys), _get_path(base, keys)
        if head_value != base_value:
            _set_path(merged, keys, base_value)
            if head_value is not None:
                name = ".".join(keys)
                warnings.append(
                    f"ignored {name} from {path.name} in the checked-out tree: policy keys are "
                    f"read from {ref!r} only. Set it on {ref!r} or through VIBEKILL_* "
                    "environment variables."
                )
    return merged, warnings


def _ref_exists(path: Path, ref: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=path.resolve().parent, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


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
