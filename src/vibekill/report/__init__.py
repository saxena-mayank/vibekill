"""Report rendering. One `ReviewResult` in, four formats out."""

from .renderer import (
    VERSION,
    render_html,
    render_json,
    render_markdown,
    render_sarif,
    write_reports,
)

__all__ = [
    "VERSION",
    "render_html",
    "render_json",
    "render_markdown",
    "render_sarif",
    "write_reports",
]
