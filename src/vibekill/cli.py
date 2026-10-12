"""Command line interface.

`vk` is a shorter alias for every command below.

    vibekill review                 review the last commit
    vibekill review --base main     review a branch against main
    vibekill review --uncommitted   review what you have not committed yet
    vibekill doctor                 check the environment and prove the air-gap
    vibekill serve                  start the API
    vibekill ui                     start the Streamlit UI
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer

from .models import Verdict
from .pipeline import SourceError, run_review, verify_airgap
from .report import VERSION, render_markdown
from .settings import Settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Kills the vibe before your diff kills production.\n\n"
        "Runs security, quality, testing and complexity agents over a git diff, "
        "fully offline by default."
    ),
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def review(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the git repository."),
    base: str = typer.Option("HEAD~1", "--base", "-b", help="Base ref to compare against."),
    head: str = typer.Option("HEAD", "--head", help="Head ref to review."),
    uncommitted: bool = typer.Option(
        False, "--uncommitted", help="Review working-tree changes instead of a commit range."
    ),
    mode: str | None = typer.Option(None, "--mode", help="airgapped (default) or github."),
    pr: int | None = typer.Option(None, "--pr", help="GitHub PR number; implies --mode github."),
    github_repo: str | None = typer.Option(None, "--github-repo", help="owner/name for GitHub mode."),
    model: str | None = typer.Option(None, "--model", "-m", help="Local model name."),
    llm_url: str | None = typer.Option(None, "--llm-url", help="Model base URL."),
    provider: str | None = typer.Option(
        None, "--provider", help="auto|ollama|openai|none. 'none' runs deterministic analysis only."
    ),
    formats: str = typer.Option("markdown,html,json", "--format", "-f", help="Comma-separated report formats."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Report output directory."),
    fail_on: str | None = typer.Option(None, "--fail-on", help="blocker|major|minor|info."),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to vibekill.toml."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only write reports, no stdout summary."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Review a change and write reports."""
    _configure_logging(verbose)

    overrides: dict = {}
    if mode or pr or github_repo:
        overrides["mode"] = mode or "github"
    if fail_on:
        overrides["fail_on"] = fail_on
    llm: dict = {}
    if model:
        llm["model"] = model
    if llm_url:
        llm["base_url"] = llm_url
    if provider:
        llm["provider"] = provider
    if llm:
        overrides["llm"] = llm
    github: dict = {}
    if pr:
        github["pr_number"] = pr
    if github_repo:
        github["repo"] = github_repo
    if github:
        overrides["github"] = github

    try:
        settings = Settings.load(config, search_from=repo, **overrides)
    except Exception as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    try:
        run = run_review(
            settings,
            repo_path=repo,
            base=base,
            head=head,
            include_uncommitted=uncommitted,
            output_dir=output,
            formats=[f.strip() for f in formats.split(",") if f.strip()],
        )
    except SourceError as exc:
        typer.secho(f"could not read the diff: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    result = run.result
    if not quiet:
        typer.echo(render_markdown(result))
        typer.echo("")

    for fmt, path in run.reports.items():
        typer.secho(f"  {fmt:<9} {path}", fg=typer.colors.BRIGHT_BLACK, err=True)

    counts = result.counts()
    colour = {
        Verdict.REQUEST_CHANGES: typer.colors.RED,
        Verdict.COMMENT: typer.colors.YELLOW,
        Verdict.APPROVE: typer.colors.GREEN,
    }[result.verdict]
    typer.secho(
        f"\n{result.verdict.value}: {counts['blocker']} blocker, {counts['major']} major, "
        f"{counts['minor']} minor, {counts['info']} info "
        f"({result.duration_s:.1f}s)",
        fg=colour,
        err=True,
    )
    raise typer.Exit(run.exit_code)


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check the environment, and prove the air-gap actually blocks egress."""
    _configure_logging(verbose)
    import shutil

    settings = Settings.load(config)
    typer.echo(f"vibekill {VERSION}\n")

    typer.echo(f"mode                {settings.mode}")
    typer.echo(f"config              {settings.model_config.get('env_prefix', '')}* env + TOML")
    typer.echo(f"air-gap enforced    {settings.enforce_airgap}")
    typer.echo(f"allowed hosts       {', '.join(settings.allow_hosts)}")
    typer.echo("")

    typer.echo("analysers")
    for tool in ("ruff", "bandit", "semgrep"):
        enabled = getattr(settings.tools, tool)
        found = shutil.which(tool)
        state = "found" if found else "not installed"
        if not enabled:
            state = "disabled in config"
        typer.echo(f"  {tool:<16} {state}")
    typer.echo(f"  {'heuristics':<16} always available")
    if settings.tools.semgrep and not settings.tools.semgrep_configs and settings.enforce_airgap:
        typer.secho(
            "  note: semgrep has no local rules configured, so it will be skipped in "
            "air-gapped mode ('--config auto' contacts the registry).",
            fg=typer.colors.YELLOW,
        )
    typer.echo("")

    typer.echo("model")
    from .pipeline import probe_model

    client = probe_model(settings)
    if client.available:
        typer.secho(f"  {client.describe()}", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"  no model reachable at {settings.llm.base_url}; reviews will use "
            "deterministic analysis only",
            fg=typer.colors.YELLOW,
        )
    typer.echo("")

    typer.echo("air-gap verification")
    ok, detail = verify_airgap(settings)
    if ok:
        typer.secho(f"  egress is blocked: {detail}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  {detail}", fg=typer.colors.RED)
    raise typer.Exit(0 if ok or not settings.enforce_airgap else 1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload for development."),
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Acknowledge the risk of exposing the unauthenticated local API.",
    ),
) -> None:
    """Start the FastAPI service.

    Binds to loopback by default. The API has no authentication: it executes
    analysers against local paths, so exposing it on a network interface without
    a reverse proxy enforcing auth would let anyone trigger that. See the
    security note in docs/API.md before changing --host.
    """
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1") and not allow_network:
        typer.secho(
            "Refusing a non-loopback bind. The API accepts server filesystem paths and "
            "has no authentication. Use --allow-network only behind an authenticating proxy.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    uvicorn.run("vibekill.api:app", host=host, port=port, reload=reload)


@app.command()
def ui(
    port: int = typer.Option(8501, "--port", "-p"),
) -> None:
    """Start the Streamlit UI."""
    import subprocess

    ui_path = Path(__file__).parent / "ui.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(ui_path),
            "--server.port",
            str(port),
            "--server.address",
            "127.0.0.1",
        ],
        check=False,
    )
    if completed.returncode:
        raise typer.Exit(completed.returncode)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(VERSION)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
