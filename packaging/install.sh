#!/bin/sh
# Offline installer for vibekill. Requires no network access.
#
# Shipped inside the offline bundle alongside every dependency wheel, so pip can
# resolve everything from this directory with --no-index.
#
#   sh install.sh                  # installs to ~/.local/vibekill
#   sh install.sh /opt/vibekill    # or anywhere you can write
#
# POSIX sh on purpose: an air-gapped host may not have bash.

set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-$HOME/.local/vibekill}"

# The bundle is built for one Python ABI. Override VIBEKILL_PYTHON only with an
# interpreter of the same major/minor version.
PYTHON="${VIBEKILL_PYTHON:-__PYTHON__}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: this bundle requires __PYTHON__ on PATH." >&2
    echo "       Download the bundle matching the target Python version." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == __PYTHON_TUPLE__ else 1)'; then
    echo "error: $PYTHON has the wrong Python ABI for this bundle." >&2
    exit 1
fi

echo "Using $($PYTHON --version)"
echo "Installing vibekill into $TARGET"

"$PYTHON" -m venv "$TARGET"

# --no-index is the whole point: pip must not attempt to reach PyPI.
"$TARGET/bin/pip" install --quiet --upgrade --no-index --find-links "$DIR" pip 2>/dev/null || true
"$TARGET/bin/pip" install --quiet --no-index --find-links "$DIR" vibekill

echo
echo "Installed vibekill $("$TARGET/bin/vibekill" version)"
echo
echo "Add it to your PATH:"
echo "    export PATH=\"$TARGET/bin:\$PATH\""
echo
echo "Then confirm nothing can leave the machine:"
echo "    vibekill doctor"
echo
echo "And review something:"
echo "    vibekill review --base main"
