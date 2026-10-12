# Troubleshooting

Start with:

```bash
vibekill doctor
```

It reports the active mode, available analyzers, model reachability, and whether
the socket guard blocks public egress.

## `not inside a git repository`

Pass the repository root, not a source subdirectory:

```bash
vibekill review --repo /path/to/repository
```

In the UI, **Path on this machine** refers to the machine running Streamlit. A
path on your laptop is not available to a UI hosted on another server.

## `HEAD~1` or merge-base fails

A repository with only one commit has no `HEAD~1`. Pick an existing base:

```bash
git log --oneline --all --decorate -10
vibekill review --base main --head HEAD
```

CI checkouts may be shallow. Fetch enough history before review:

```bash
git fetch --unshallow || git fetch --depth=100
```

For GitHub Actions, use `actions/checkout` with `fetch-depth: 0`.

## No files were reviewed

Check what Git sees:

```bash
git diff --stat HEAD~1 HEAD
```

For local edits use `vibekill review --uncommitted`. It includes staged,
unstaged, and untracked text files. Generated, vendored, lock, binary, and very
large files may be excluded; skipped files are named in the report.

## The model is unavailable

This is not fatal. Vibekill runs deterministic checks and marks all model agents
as skipped.

For Ollama:

```bash
ollama list
curl http://127.0.0.1:11434/api/tags
vibekill review --provider ollama --model qwen2.5-coder:7b
```

For llama.cpp, vLLM, or LM Studio:

```bash
curl http://127.0.0.1:8080/v1/models
vibekill review --provider openai --llm-url http://127.0.0.1:8080 --model local
```

If the endpoint is not loopback, add its hostname to `allow_hosts` in
`vibekill.toml`. A remote model means source excerpts leave the machine.

## Semgrep is skipped

Air-gapped mode refuses `semgrep --config auto`, because it contacts the Semgrep
registry. Download rules on a connected machine, copy them into the environment,
and configure a local path:

```toml
[tools]
semgrep_configs = ["/opt/vibekill-rules"]
```

Ruff, Bandit, and Semgrep are optional. Missing tools never stop built-in checks.

## Reports cannot be written

The default output is `<repository>/.vibekill`. Use a writable location when the
repository is read-only:

```bash
vibekill review --repo /repo --output /reports
```

This is required for Docker when `/repo` is mounted `:ro`.

## GitHub returns 401, 403, or rate-limit errors

Set `GITHUB_TOKEN` (or `VIBEKILL_GITHUB__TOKEN`). For a fine-grained token,
read-only **Contents** and **Pull requests** access is enough.

```bash
export GITHUB_TOKEN=github_pat_...
vibekill review --mode github --github-repo owner/repository --pr 42
```

GitHub mode is fetch-only in v0.1 and never posts comments.

## A finding is missing

Model findings are intentionally dropped when they lack an exact path, changed
line number, or matching source quote. The report shows `unverified dropped` and
`low-confidence hidden` counts. This is a precision-first choice: invisible noise
is safer than a confident comment on code that does not exist.

Deterministic findings are also limited to changed lines by default. Set
`tools.changed_lines_only = false` only when intentionally auditing whole files.

## The offline bundle will not install

Bundle filenames include their Python ABI and platform, for example:

```
vibekill-0.1.0-offline-py312-linux-x86_64.tar.gz
```

Use a bundle matching the target's Python major/minor, OS, and architecture.
Compiled wheels are not portable between Python 3.11, 3.12, and 3.13. Build a
matching bundle on a connected machine with `make bundle` when needed.

## Still stuck?

Collect these without including proprietary source:

```bash
vibekill version
python --version
git --version
vibekill doctor
```

Open an issue for functional bugs. Use the private process in
[SECURITY.md](../SECURITY.md) for egress bypasses, unintended file access, command
execution, or credential exposure.
