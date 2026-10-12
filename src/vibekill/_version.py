"""Single source of truth for the version.

`pyproject.toml` holds the number; everything else reads it from the installed
package metadata. Previously it was written out in three places, which is exactly
the kind of duplication that ships a wheel labelled 0.2.0 with a report footer
claiming 0.1.0.

This module imports nothing from the rest of the package, so `report/` can read
the version without creating a cycle through `__init__`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

try:
    __version__ = _metadata_version("vibekill")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
