"""Local FastAPI interface.

The API is intentionally synchronous and stateless. Each connection remains open
until its review finishes; callers that need asynchronous execution should queue
requests outside this process.

The service accepts local filesystem paths, so it binds to loopback by default.
See ``docs/API.md`` before exposing it through a reverse proxy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .pipeline import SourceError, probe_model, run_review, verify_airgap
from .report import VERSION, render_html, render_markdown, render_sarif
from .settings import Settings

app = FastAPI(
    title="vibekill",
    version=VERSION,
    summary="Kills the vibe before your diff kills production.",
    description=(
        "Local-first agentic code review. Requests run one at a time because the "
        "air-gap guard is process-wide. This API has no built-in authentication; "
        "keep it on loopback or place an authenticating proxy in front of it."
    ),
)


class ReviewRequest(BaseModel):
    """Inputs shared by the local and GitHub source adapters."""

    repo_path: str = Field(default=".", description="Path on the API server.")
    base: str = Field(default="HEAD~1", description="Base git ref.")
    head: str = Field(default="HEAD", description="Head git ref.")
    include_uncommitted: bool = False

    mode: Literal["airgapped", "github"] = "airgapped"
    github_repo: str | None = Field(default=None, description="owner/name")
    pr_number: int | None = Field(default=None, ge=1)

    provider: Literal["auto", "ollama", "openai", "none"] | None = None
    model: str | None = None
    llm_base_url: str | None = None
    fail_on: Literal["blocker", "major", "minor", "info"] | None = None

    def settings(self) -> Settings:
        overrides: dict[str, Any] = {"mode": self.mode}
        if self.fail_on:
            overrides["fail_on"] = self.fail_on

        llm = {
            key: value
            for key, value in {
                "provider": self.provider,
                "model": self.model,
                "base_url": self.llm_base_url,
            }.items()
            if value is not None
        }
        if llm:
            overrides["llm"] = llm

        if self.mode == "github":
            overrides["github"] = {
                "repo": self.github_repo or "",
                "pr_number": self.pr_number,
            }

        search_from = Path(self.repo_path) if self.mode == "airgapped" else None
        return Settings.load(search_from=search_from, **overrides)


def _review(request: ReviewRequest):
    if request.mode == "airgapped" and not Path(request.repo_path).exists():
        raise HTTPException(status_code=400, detail=f"repo_path does not exist: {request.repo_path}")

    if request.llm_base_url:
        from urllib.parse import urlparse

        server_policy = Settings.load()
        requested_host = urlparse(request.llm_base_url).hostname or ""
        if requested_host not in server_policy.allow_hosts:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"model host {requested_host!r} is not allowed by server policy; "
                    "add it to VIBEKILL_ALLOW_HOSTS on the server"
                ),
            )
    try:
        return run_review(
            request.settings(),
            repo_path=request.repo_path,
            base=request.base,
            head=request.head,
            include_uncommitted=request.include_uncommitted,
            write=False,
        ).result
    except SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/health", summary="Check service and model availability")
def health() -> dict[str, Any]:
    settings = Settings.load()
    client = probe_model(settings)
    return {
        "status": "ok",
        "version": VERSION,
        "mode": settings.mode,
        "airgap_enforced": settings.enforce_airgap,
        "model": client.describe(),
        "model_available": client.available,
        "concurrency": "one review per process",
    }


@app.get("/airgap", summary="Prove public egress is blocked")
def airgap() -> dict[str, Any]:
    settings = Settings.load()
    ok, detail = verify_airgap(settings)
    return {"enforced": settings.enforce_airgap, "verified": ok, "detail": detail}


@app.post("/reviews", summary="Run a review and return JSON")
def create_review(request: ReviewRequest) -> dict[str, Any]:
    """Run one review. The connection stays open until analysis finishes."""
    return _review(request).to_dict()


@app.post("/reviews/report", response_class=HTMLResponse, summary="Run and return HTML")
def create_html_report(request: ReviewRequest) -> HTMLResponse:
    return HTMLResponse(render_html(_review(request)))


@app.post("/reviews/report.md", summary="Run and return Markdown")
def create_markdown_report(request: ReviewRequest) -> Response:
    return Response(render_markdown(_review(request)), media_type="text/markdown")


@app.post("/reviews/report.sarif", summary="Run and return SARIF")
def create_sarif_report(request: ReviewRequest) -> Response:
    return Response(render_sarif(_review(request)), media_type="application/sarif+json")
