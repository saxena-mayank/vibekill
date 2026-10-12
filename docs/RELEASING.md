# Releasing

Three artifacts go out per release:

| Artifact | Where | For whom |
|---|---|---|
| wheel + sdist | PyPI | everyone: `pipx install vibekill` |
| wheel + sdist | GitHub release | pinning a URL, or no PyPI access |
| offline bundles | GitHub release | air-gapped machines; tagged by Python ABI and platform |

The offline bundles are the ones that matter for this project's core audience. A
wheel alone is not installable without network, because pip still resolves
dependencies from PyPI. Each bundle carries every transitive wheel plus an
installer that runs `pip --no-index`.

## One-time setup

### PyPI Trusted Publishing

No API token is stored in the repository. PyPI verifies a short-lived OIDC token
minted by GitHub for this specific workflow, so there is no long-lived credential
to leak or rotate.

At <https://pypi.org/manage/account/publishing/>, add a pending publisher:

| Field | Value |
|---|---|
| PyPI project name | `vibekill` |
| Owner | your GitHub org or username |
| Repository name | `vibekill` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Then create a `pypi` environment in the repository settings. Adding a required
reviewer there is worth it: a PyPI release cannot be deleted or replaced, only
yanked, so a human gate before the irreversible step is cheap insurance.

### Repository metadata

Before the first release, add the real `Homepage`, `Documentation`, and `Issues`
URLs under `[project.urls]` in `pyproject.toml`. They are intentionally omitted
rather than published with fake placeholder links.

## Cutting a release

```bash
# 1. Bump the version. One place only; everything else reads it from metadata.
$EDITOR pyproject.toml          # version = "0.2.0"

# 2. Everything that must pass.
make release-check              # lint + tests + build + install the wheel and run it

# 3. Build the bundles locally if you want to inspect them.
make bundle
make bundle-full

# 4. Tag and push. The workflow does the rest.
git commit -am "Release 0.2.0"
git tag v0.2.0
git push origin main v0.2.0
```

The workflow refuses to publish when the tag and `pyproject.toml` disagree. That
check exists because pushing `v0.2.0` against version `0.1.0` would put the wrong
number on PyPI permanently.

## What the workflow does

1. Verifies the tag matches `pyproject.toml`
2. Runs lint and the test suite
3. Builds the wheel and sdist, runs `twine check`
4. Asserts `report/template.html` is inside the wheel — omitting it breaks HTML
   reports only for installed copies, which a source-tree test suite cannot catch
5. Installs the wheel in a clean venv and checks `vibekill version` matches
6. Builds both offline bundles
7. Publishes to PyPI via Trusted Publishing
8. Creates the GitHub release with all four files attached

## Verifying by hand

```bash
make verify-wheel       # clean venv, install the wheel, run a real review with it

# Simulate an air-gapped target: --no-index means pip cannot reach PyPI
make bundle
mkdir -p /tmp/sim && tar -xzf dist/vibekill-*-offline-*.tar.gz -C /tmp/sim
sh /tmp/sim/install.sh /tmp/sim/venv
/tmp/sim/venv/bin/vibekill doctor
```

## Versioning

Semantic versioning, with the caveat that pre-1.0 minor bumps may change
behaviour. Things that count as breaking once 1.0 lands:

- renaming or removing a `rule_id` (they appear in SARIF and in users' baselines)
- changing the `schema` value in JSON output
- renaming a config key or environment variable
- changing what `fail_on` does, or which severity a rule reports by default

Prompt changes are *not* breaking, but they do change what users see. Note them in
the release notes, because a finding count that jumps without explanation looks
like a bug.

## If a release goes wrong

PyPI files cannot be replaced. Yank the release and ship a patch version:

```bash
# marks it as not-for-new-installs while leaving existing pins working
pip install twine && twine yank vibekill==0.2.0    # or use the PyPI web UI
```

Then fix, bump to `0.2.1`, and tag again.
