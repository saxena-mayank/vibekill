# Local API

Vibekill exposes the same review engine used by its CLI and Streamlit UI through
FastAPI.

## Start it

```bash
vibekill serve
# API:  http://127.0.0.1:8000
# Docs: http://127.0.0.1:8000/docs
```

The service is synchronous and stateless. One request stays open until its review
finishes, and one review runs per process. If your caller needs asynchronous
execution, put a durable queue in front of Vibekill.

## Security boundary

The API accepts a repository path **on the server** and invokes Git and optional
analyzers against that path. It intentionally has no authentication in v0.1.
Consequently:

- it binds to loopback by default;
- `vibekill serve --host 0.0.0.0` is refused unless `--allow-network` is also set;
- `--allow-network` is an acknowledgement, not authentication—use it only behind
  a reverse proxy that authenticates callers and restricts repository access;
- request-supplied model URLs must already appear in the server's
  `VIBEKILL_ALLOW_HOSTS`; callers cannot authorize a new egress destination;
- report responses may contain source snippets, secrets found in source, internal
  paths, and model output. Apply the same retention controls as the repository.

Do not expose the development server directly to an office network or the
internet.

## Review JSON

```bash
curl -X POST http://127.0.0.1:8000/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_path": "/srv/repos/payments",
    "base": "main",
    "head": "HEAD",
    "provider": "none"
  }'
```

Important fields:

| Field | Default | Meaning |
|---|---|---|
| `repo_path` | `.` | Git repository path on the API server |
| `base` | `HEAD~1` | Base ref; Vibekill reviews from its merge-base |
| `head` | `HEAD` | Head ref |
| `include_uncommitted` | `false` | Review staged, unstaged, and untracked text files |
| `mode` | `airgapped` | `airgapped` or `github` |
| `provider` | config | `auto`, `none`, `ollama`, or `openai` |
| `model` | config | Model name at the configured endpoint |
| `llm_base_url` | config | Ollama or OpenAI-compatible endpoint |
| `fail_on` | `blocker` | Verdict threshold |

The response follows `vibekill/review/v1`. It includes the verdict, findings,
agent and analyzer status, policy, and coverage.

## Reports

Use the same JSON body with one of these endpoints:

| Endpoint | Media type |
|---|---|
| `POST /reviews/report` | self-contained HTML |
| `POST /reviews/report.md` | Markdown |
| `POST /reviews/report.sarif` | SARIF 2.1.0 |

```bash
curl -X POST http://127.0.0.1:8000/reviews/report \
  -H 'Content-Type: application/json' \
  -d '{"repo_path":"/srv/repos/payments","base":"main","provider":"none"}' \
  -o review.html
```

Each report request runs a new review. If you need all formats from one run, use
the CLI (`--format markdown,html,json,sarif`) or call the Python API and render
one `ReviewResult` several ways.

## GitHub PR source

```bash
export GITHUB_TOKEN=github_pat_...   # required for private repositories
curl -X POST http://127.0.0.1:8000/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "github",
    "github_repo": "owner/repository",
    "pr_number": 42,
    "provider": "none"
  }'
```

A fine-grained read-only token with **Contents** and **Pull requests** access is
enough. GitHub mode is fetch-only in v0.1; it does not post review comments.

The socket guard remains active and allowlists the GitHub API plus the configured
model endpoint. A remote model URL is therefore explicit code egress; use a
loopback or private endpoint to keep inference local.

## Health checks

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/airgap
```

`/airgap` actively attempts public egress and reports whether the guard refused
it before a connection was made.
