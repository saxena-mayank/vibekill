<h1 align="center">💀 vibekill</h1>

<p align="center"><strong>Kills the vibe before your diff kills production.</strong></p>

<p align="center">
  Local-first agentic code review, built for the way AI-generated code actually fails.<br>
  Four agents. One report. Public egress blocked in local mode.
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue">
  <img alt="Licence Apache-2.0" src="https://img.shields.io/badge/licence-Apache--2.0-green">
  <img alt="Runs offline" src="https://img.shields.io/badge/network-none%20required-black">
</p>

---

## The problem

Vibe-coded PRs don't look like bad code. That's the problem.

They have clean names, docstrings, type hints, and a test file. They pass CI. They
read like something a careful engineer wrote on a good day. So they get approved in
forty seconds with a 👍, because there is nothing to *see*.

Then you find out:

- the input validation guards a path nothing reaches, and the actual sink is three
  lines lower, unguarded
- `assert result is not None` is the entire test, so it passes no matter what the
  function returns
- an exception is caught and dropped, so the failure surfaces two services away
  next Tuesday
- there is a `UserRepositoryFactory` with one implementation, and a `retry_call`
  helper that duplicates the `retry` already sitting in `utils/`
- the whole thing is 300 lines where 40 would do

None of that is a typo a linter catches. All of it is what a model produces when
you ask for a feature and it decides to be thorough.

**The volume is the part people underestimate.** Extra code isn't free — it gets
read, tested, debugged and migrated for as long as the project lives. A PR that is
five times larger than the problem it solves is a bill your team pays every
quarter, forever, and nobody ever sends the invoice.

## What vibekill does

```bash
pipx install vibekill
vibekill review --base main
```

Four agents review the diff in parallel:

| Agent | What it hunts |
|---|---|
| 🔓 **Security** | injection, missing authorisation, secrets, crypto misuse, unsafe deserialisation |
| 🐛 **Code quality** | logic bugs, swallowed errors, resource leaks, broken contracts |
| 🧪 **Testing** | tests that would pass even if the code were wrong |
| ✂️ **Complexity** | code that is bigger or more abstract than the problem needs |

That fourth one is why this exists. Every review bot finds SQL injection. Almost
none will tell you that your factory has one implementation, or that you just
rewrote a helper that already exists two directories over.

### Works before you install a model

```
$ vibekill review --base main

Changes requested · 2 blocker, 3 major, 5 minor

app/users.py
  🛑 :3   Possible hardcoded credential
  🛑 :9   SQL query built by string interpolation
  ⚠️ :17  Exception silently swallowed
  •  :6   `find_user` ignores `locale`, `include_deleted`
  •  :21  `retry_call` ignores `backoff`, `jitter`, `logger`

tests/test_users.py
  •  :5   Weak assertion in `test_find_user` — only checks the result is not None
  •  :9   `test_placeholder` asserts a constant that is always true
```

No GPU, no model, no network. Deterministic analysers and built-in checks do that
much on their own. Add a local model and the agents layer judgement on top:
reachability, whether a test constrains anything, whether an abstraction is
earned.

## Local mode: nothing leaves your machine

Most tools say "we don't store your code." vibekill enforces it at the socket
layer, and you can check:

```
$ vibekill doctor

air-gap verification
  egress is blocked: a connection to example.com:443 was refused by the guard
  before any packet left the process
```

While a local review runs, every outbound connection to a non-allowlisted host raises.
DNS resolution too — a lookup is already traffic leaving the building. Child
analysers get a scrubbed environment with `GITHUB_TOKEN` and friends stripped out.
`semgrep --config auto` is refused, because it tells the Semgrep registry your
languages, frameworks and project URL.

Your proprietary diff cannot reach a model vendor, because the socket won't open.

## Install

```bash
pipx install vibekill          # recommended: isolated, and puts vibekill on PATH
pip install vibekill           # or into an environment you manage
pip install "vibekill[ui]"     # adds the Streamlit UI
```

Optional deterministic analysers. vibekill works without them, and tells you which
are missing:

```bash
pipx inject vibekill ruff bandit semgrep
```

### Air-gapped install

The machine that runs reviews never needs network — but `pip install` does, so a
bare wheel is not enough. Download the offline bundle from this repository's
**Releases** page; it carries vibekill plus every dependency as wheels.

```bash
# after downloading the bundle on a connected machine, carry it across
mkdir vibekill-install
# VERSION is the release you downloaded
tar -xzf vibekill-VERSION-offline-py312-linux-x86_64.tar.gz -C vibekill-install
sh vibekill-install/install.sh ~/.local/vibekill
export PATH="$HOME/.local/vibekill/bin:$PATH"

vibekill doctor    # confirms egress is blocked
```

Nothing in that path touches PyPI — the installer runs `pip --no-index`.

| Bundle | Size | Contents |
|---|---|---|
| `vibekill-VERSION-offline-py312-linux-x86_64.tar.gz` | ~20 MB | CLI and API |
| `vibekill-VERSION-offline-full-py312-linux-x86_64.tar.gz` | ~123 MB | adds the Streamlit UI |

The size difference is entirely Streamlit's dependency tree — pandas, pyarrow and
numpy. Take the small one unless you need the UI on that machine.

Release bundles are built for Python 3.12 on Linux x86_64 because compiled
wheels are Python-ABI and platform-specific. For any other target, build on a
matching connected machine with `make bundle`; the generated filename records
its Python and platform tag.

### Docker

```bash
docker build -t vibekill:local .
mkdir -p reports
docker run --rm --network none \
  -v "$PWD:/repo:ro" -v "$PWD/reports:/reports" \
  vibekill:local review --repo /repo --base main --provider none --output /reports
```

`--network none` gives you a hard guarantee at the kernel level; the socket guard
inside the process is then the second layer.

## Quickstart

```bash
vibekill review                  # the last commit
vibekill review --base main      # a branch against main
vibekill review --uncommitted    # what you haven't committed yet
vibekill doctor                  # environment check + prove egress is blocked
```

`vk` works as a shorter alias for all of it. Reports land in `.vibekill/` as
`review.md`, `review.html` and `review.json`.

Working on a source checkout:

```bash
cd vibekill
make install     # venv + dev extras + analysers
make demo        # review a repo seeded with deliberate slop
```

### Add a local model

```bash
ollama pull qwen2.5-coder:7b
vibekill review --model qwen2.5-coder:7b

# or any OpenAI-compatible server: llama.cpp, vLLM, LM Studio, TGI
vibekill review --provider openai --llm-url http://127.0.0.1:8080 --model local
```

### UI and API

```bash
vibekill ui       # Streamlit at http://127.0.0.1:8501
vibekill serve    # FastAPI at http://127.0.0.1:8000, docs at /docs
```

```bash
curl -X POST http://127.0.0.1:8000/reviews \
  -H 'Content-Type: application/json' \
  -d '{"repo_path": "/path/to/repo", "base": "main", "provider": "none"}'
```

### API

The local API is synchronous and stateless: one request remains open until its
review finishes, and one review runs per process. Keep it on loopback unless an
authenticating proxy restricts both callers and repository paths. See
[docs/API.md](docs/API.md).

> ⚠️ **The API has no authentication** and accepts filesystem paths on the server.

### CI

```yaml
permissions:
  contents: read
  security-events: write

steps:
  - run: pip install vibekill
  - run: vibekill review --base "origin/${{ github.base_ref }}" --format markdown,sarif
  - uses: github/codeql-action/upload-sarif@v3
    with:
      sarif_file: .vibekill/review.sarif
```

Exits non-zero when something at or above `fail_on` (default `blocker`) survives.

## Two modes, one engine

|  | Air-gapped (default) | GitHub-connected |
|---|---|---|
| Diff from | local git, `merge-base(base, head)` | GitHub PR API |
| Results to | `.vibekill/` + stdout | returned locally; fetch-only in v0.1 |
| Network | everything blocked but allowlisted hosts | GitHub API + configured model host only |

```bash
export GITHUB_TOKEN=ghp_...
vibekill review --mode github --github-repo owner/name --pr 42
```

**GitHub mode is not "cloud mode."** The model still runs wherever you put it.
Self-hosted runners plus a local Qwen gives you PR analysis and SARIF annotations
while no code reaches a model vendor. Vibekill v0.1 is fetch-only and never posts
comments or review state back to GitHub.

## Why it isn't noisy

A review bot that cries wolf gets muted in two weeks, and a muted bot catches
nothing. Precision is chosen over recall everywhere:

**Every model finding must quote the line it cites.** The aggregator reads the real
line and compares. No match, it's dropped and counted. A 7B model *will*
confidently cite line 412 of an 80-line file — this makes that harmless instead of
embarrassing.

**No model blocks a merge on its own.** `blocker` requires a deterministic
analyser to agree, or a verified high-confidence security finding. Everything else
caps at `major`.

**Changed lines only.** Touching one line in a legacy file will not bury you in
pre-existing issues you didn't cause.

**Failures are visible.** "No findings" and "the security agent timed out" are
different facts, and the report says which.

Every review tells you what it threw away:

```
3 findings discarded as unverifiable, 7 merged as duplicates, 0 below the
confidence floor.
```

## How it works

```
  source adapter  ->  ReviewContext
                          |
                 deterministic analysis          ruff · bandit · semgrep
                 (parallel processes)            + built-in checks
                          |
                    Evidence Pack
                          |
        security · quality · testing · complexity      (LangGraph, concurrent)
                          |
       evidence gate -> dedup -> severity policy -> verdict
                          |
          markdown · HTML · JSON · SARIF
```

Analysers run **first**, and their output is the agents' input. The model's job is
triage and judgement — "semgrep flagged this sink; is it reachable given the check
three lines up?" — not detection from scratch. That is a far easier task for a
model small enough to run on your own hardware.

Full design in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

<p align="center">
  <img src="docs/architecture.svg" alt="Vibekill architecture: local Git or GitHub PR input, deterministic analysis, four parallel agents, evidence-gated aggregation, and UI/API/CLI/report outputs" width="100%">
</p>

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — components, data flow, and design trade-offs
- [Local API](docs/API.md) — endpoints, security boundary, and examples
- [Troubleshooting](docs/TROUBLESHOOTING.md) — Git, models, analyzers, Docker, and offline bundles
- [Contributing](CONTRIBUTING.md) — add a check, agent, source, or analyzer
- [Security policy](SECURITY.md) — deployment guidance and private disclosure
- [Releasing](docs/RELEASING.md) — PyPI, wheels, and air-gapped bundles

## Configuration

`vibekill.toml` in your repo root, or `VIBEKILL_*` environment variables. Every
option is in [vibekill.example.toml](vibekill.example.toml).

```toml
fail_on = "blocker"

[llm]
provider = "auto"          # auto | ollama | openai | none
model = "qwen2.5-coder:7b"

[agents.complexity]
extra_instructions = "This codebase prefers explicit code over clever code."
```

## What it won't do

Stated plainly, because a tool that oversells itself gets uninstalled:

- **Four agents, but not a 4× speedup.** They run concurrently; their model calls
  queue behind a semaphore of 2, because one local runtime doesn't go faster with
  four callers waiting on it.
- **The socket guard covers Python, not arbitrary subprocesses.** For a hard
  boundary run the container with `--network none`; the guard is the second layer.
- **Reinvention detection is shallow.** The complexity agent sees repo file paths,
  so it catches "there's already a `utils/retry.py`". No semantic similarity yet.
- **Duplicate detection is near-literal.** Copy-paste with renamed variables gets
  past the built-in check and relies on the agent.
- **Python is best supported.** AST-level checks are Python-only today; other
  languages get regex checks plus the agents.
- **It is advisory.** A finding is a prompt to look, not a verdict. It will not
  catch every bug and will occasionally flag something you disagree with.

## Contributing

A new check is one function. A new agent is about forty lines. See
[CONTRIBUTING.md](CONTRIBUTING.md).

```bash
make test      # 100+ tests, no network
make lint
make demo      # review a repo seeded with deliberate slop
```

Good first issues are listed at the bottom of CONTRIBUTING. The most valuable one
is the evaluation harness — right now prompt tuning is guesswork, and a labelled
corpus of diffs would fix that for everybody.

## Licence

Apache-2.0. See [LICENSE](LICENSE).

---

<p align="center"><sub>
Ship the feature. Kill the vibe.
</sub></p>
