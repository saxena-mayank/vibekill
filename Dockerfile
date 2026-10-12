# vibekill container image.
#
# Build:
#   docker build -t vibekill:0.1.0 .
#
# The strongest air-gap guarantee available: no network interface at all, so the
# socket guard is defence in depth rather than the only boundary.
#
#   mkdir -p reports
#   docker run --rm --network none \
#       -v /path/to/repo:/repo:ro -v "$PWD/reports:/reports" \
#       vibekill:0.1.0 review --repo /repo --base main --provider none --output /reports
#
# To use a model on the host, prefer a private bridge and explicitly allow only
# its hostname. Host networking also exposes analyser subprocesses and is not
# equivalent to `--network none`.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Dependencies are installed from the pinned lockfile in a separate layer so
# source edits do not trigger a full reinstall.
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -q -r requirements.txt

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src/ ./src/
RUN /opt/venv/bin/pip install -q --no-deps .


FROM python:3.12-slim

# git is required: the local source adapter shells out to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SEMGREP_SEND_METRICS=off \
    DO_NOT_TRACK=1

# Non-root. The repository can be mounted read-only when reports are written to
# a separate writable volume with `--output /reports`.
RUN useradd --create-home --uid 10001 vibekill
USER vibekill
WORKDIR /repo

# Repositories are mounted, and git refuses to operate on a tree owned by
# another uid unless told it is safe.
RUN git config --global --add safe.directory '*'

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python -c "import vibekill" || exit 1

ENTRYPOINT ["vibekill"]
CMD ["--help"]
