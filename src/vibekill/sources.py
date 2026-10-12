"""Where a diff comes from.

Two adapters, one output type. `LocalGitSource` shells out to git and never
touches the network; `GitHubPRSource` fetches a pull request over HTTPS. Both
return a `ReviewContext`, and everything downstream is identical for the two —
that shared contract is what keeps the air-gapped and connected modes from
quietly drifting apart.

Adding a third source (GitLab, Gerrit, a patch file on disk) means implementing
one method. See docs/CONTRIBUTING.md.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from .diffparse import filter_files, parse_unified_diff, truncate_to_budget
from .models import FileDiff, RepoMeta, ReviewContext
from .settings import Settings

# Reading whole files into prompts is only safe for small ones; a single
# generated file would otherwise evict the rest of the diff from the context.
FULL_FILE_MAX_CHARS = 20_000
REPO_INDEX_MAX_ENTRIES = 2_000


class SourceError(RuntimeError):
    """Raised when a diff cannot be obtained. Always actionable for the user."""


class ReviewSource(Protocol):
    """The seam between "where the change lives" and "how it is reviewed"."""

    def fetch(self) -> ReviewContext: ...


# --------------------------------------------------------------------------- #
# Local git
# --------------------------------------------------------------------------- #


class LocalGitSource:
    """Reads a diff from a git working copy. The default, fully offline path."""

    def __init__(
        self,
        settings: Settings,
        repo_path: Path | str = ".",
        base: str = "HEAD~1",
        head: str = "HEAD",
        include_uncommitted: bool = False,
    ) -> None:
        self.settings = settings
        self.repo_path = Path(repo_path).resolve()
        self.base = base
        self.head = head
        self.include_uncommitted = include_uncommitted

    # -- git plumbing --------------------------------------------------- #

    def _git(self, *args: str, check: bool = True) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SourceError("git is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceError(f"git {' '.join(args)} timed out") from exc
        if check and proc.returncode != 0:
            raise SourceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _repo_root(self) -> Path:
        root = self._git("rev-parse", "--show-toplevel").strip()
        if not root:
            raise SourceError(f"{self.repo_path} is not inside a git repository")
        return Path(root)

    def _rev(self, ref: str) -> str:
        return self._git("rev-parse", ref).strip()

    def _merge_base(self, base: str, head: str) -> str:
        """The fork point, so the review covers only this branch's work.

        Diffing base..head directly would also surface everything that landed on
        the base branch since the split, which is somebody else's code.
        """
        out = self._git("merge-base", base, head, check=False).strip()
        return out or self._rev(base)

    # -- public --------------------------------------------------------- #

    def fetch(self) -> ReviewContext:
        root = self._repo_root()
        self.repo_path = root

        if self.include_uncommitted:
            has_head = bool(self._git("rev-parse", "--verify", "HEAD", check=False).strip())
            if has_head:
                raw = self._git(
                    "--no-pager",
                    "diff",
                    f"--unified={self.settings.diff_context_lines}",
                    "HEAD",
                    "--no-color",
                )
                files = parse_unified_diff(raw)
                base_sha = self._rev("HEAD")
                base_ref = "HEAD"
                untracked, skipped = self._untracked_files(root)
                files.extend(untracked)
            else:
                files, skipped = self._unborn_files(root)
                base_sha = ""
                base_ref = "empty tree"
            head_sha = "working-tree"
            head_ref = "working tree"
        else:
            base_sha = self._merge_base(self.base, self.head)
            head_sha = self._rev(self.head)
            raw = self._git(
                "--no-pager",
                "diff",
                f"--unified={self.settings.diff_context_lines}",
                "--find-renames",
                base_sha,
                head_sha,
                "--no-color",
            )
            files = parse_unified_diff(raw)
            skipped = []
            base_ref, head_ref = self.base, self.head

        meta = RepoMeta(
            root=str(root),
            base_ref=base_ref,
            head_ref=head_ref,
            base_sha=base_sha,
            head_sha=head_sha,
            branch=self._git("symbolic-ref", "--short", "HEAD", check=False).strip(),
            title=self._latest_subject(),
            commit_subjects=self._commit_subjects(base_sha, head_sha),
        )

        context = _finalise(files, meta, self.settings, mode="airgapped")
        context.skipped_files.extend(skipped)
        _attach_file_contents(context, root)
        context.repo_file_index = self._file_index()
        return context

    def _latest_subject(self) -> str:
        return self._git("log", "-1", "--pretty=%s", check=False).strip()

    def _commit_subjects(self, base_sha: str, head_sha: str) -> list[str]:
        if head_sha == "working-tree":
            return []
        out = self._git("log", "--pretty=%s", f"{base_sha}..{head_sha}", check=False)
        return [line.strip() for line in out.splitlines() if line.strip()][:50]

    def _file_index(self) -> list[str]:
        """Tracked paths used by the complexity agent for overlap hints."""
        out = self._git("ls-files", check=False)
        paths = [line.strip() for line in out.splitlines() if line.strip()]
        return paths[:REPO_INDEX_MAX_ENTRIES]

    def _untracked_files(self, root: Path) -> tuple[list[FileDiff], list[str]]:
        paths = self._git("ls-files", "--others", "--exclude-standard", "-z", check=False)
        return self._paths_as_additions(root, filter(None, paths.split("\0")))

    def _unborn_files(self, root: Path) -> tuple[list[FileDiff], list[str]]:
        """Review the current contents when the repository has no first commit."""
        paths = self._git(
            "ls-files", "--cached", "--others", "--exclude-standard", "-z", check=False
        )
        return self._paths_as_additions(root, dict.fromkeys(filter(None, paths.split("\0"))))

    def _paths_as_additions(
        self, root: Path, paths: Iterable[str]
    ) -> tuple[list[FileDiff], list[str]]:
        """Represent bounded text paths as new-file diffs."""
        files: list[FileDiff] = []
        skipped: list[str] = []
        limit = min(self.settings.max_diff_chars, 100_000)

        for relative in paths:
            path = root / relative
            try:
                data = path.read_bytes()
            except OSError:
                skipped.append(relative)
                continue
            if len(data) > limit or b"\0" in data:
                skipped.append(relative)
                continue
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            body = "\n".join(f"+{line}" for line in lines)
            raw = (
                f"diff --git a/{relative} b/{relative}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{relative}\n"
                f"@@ -0,0 +1,{len(lines)} @@\n{body}\n"
            )
            files.extend(parse_unified_diff(raw))
        return files, skipped


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #


class GitHubPRSource:
    """Fetches a pull request diff from the GitHub API.

    Only reachable when `mode="github"`, because the air-gap guard blocks the
    request otherwise. Note this says nothing about where the *model* runs: a
    self-hosted runner can review PRs with a local model and never send code to
    a model vendor.
    """

    def __init__(self, settings: Settings, repo: str = "", pr_number: int | None = None) -> None:
        self.settings = settings
        self.repo = repo or settings.github.repo
        self.pr_number = pr_number or settings.github.pr_number
        if not self.repo or "/" not in self.repo:
            raise SourceError("github.repo must be set as 'owner/name'")
        if not self.pr_number:
            raise SourceError("github.pr_number must be set")

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.settings.github.token:
            headers["Authorization"] = f"Bearer {self.settings.github.token}"
        return headers

    def fetch(self) -> ReviewContext:
        import httpx

        base = f"{self.settings.github.api_url}/repos/{self.repo}/pulls/{self.pr_number}"
        timeout = self.settings.github.timeout_s
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                meta_resp = client.get(base, headers=self._headers("application/vnd.github+json"))
                meta_resp.raise_for_status()
                pr = meta_resp.json()

                diff_resp = client.get(base, headers=self._headers("application/vnd.github.v3.diff"))
                diff_resp.raise_for_status()
                raw_diff = diff_resp.text
        except httpx.HTTPStatusError as exc:
            detail = _github_error_detail(exc)
            raise SourceError(
                f"GitHub API returned {exc.response.status_code} for {self.repo}#{self.pr_number}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"could not reach the GitHub API: {exc}") from exc

        files = parse_unified_diff(raw_diff)
        meta = RepoMeta(
            root=self.repo,
            base_ref=(pr.get("base") or {}).get("ref", ""),
            head_ref=(pr.get("head") or {}).get("ref", ""),
            base_sha=(pr.get("base") or {}).get("sha", ""),
            head_sha=(pr.get("head") or {}).get("sha", ""),
            title=pr.get("title") or "",
            description=pr.get("body") or "",
            author=((pr.get("user") or {}).get("login")) or "",
            branch=(pr.get("head") or {}).get("ref", ""),
            pr_number=self.pr_number,
            remote_url=pr.get("html_url") or "",
        )
        return _finalise(files, meta, self.settings, mode="github")


def _github_error_detail(exc: object) -> str:
    try:
        payload = exc.response.json()  # type: ignore[attr-defined]
        return str(payload.get("message") or payload)[:200]
    except Exception:
        return "no further detail"


# --------------------------------------------------------------------------- #
# Shared post-processing
# --------------------------------------------------------------------------- #


def _finalise(
    files: list[FileDiff],
    meta: RepoMeta,
    settings: Settings,
    *,
    mode: str,
) -> ReviewContext:
    """Apply exclusions and budget caps, then wrap in a `ReviewContext`."""
    kept, excluded = filter_files(files, exclude=settings.exclude)
    kept, skipped = truncate_to_budget(kept, settings.max_files, settings.max_diff_chars)
    context = ReviewContext(meta=meta, files=kept, mode=mode)  # type: ignore[arg-type]
    context.excluded_files = excluded
    if skipped:
        context.skipped_files = skipped
    return context


def _attach_file_contents(context: ReviewContext, root: Path) -> None:
    """Read small files for local AST heuristics."""
    for file_diff in context.files:
        if file_diff.is_binary or file_diff.change_type == "deleted":
            continue
        path = root / file_diff.path
        try:
            if not path.is_file() or path.stat().st_size > FULL_FILE_MAX_CHARS:
                continue
            file_diff.new_content = path.read_text("utf-8", errors="replace")
        except OSError:
            continue


def build_source(
    settings: Settings,
    repo_path: Path | str = ".",
    base: str = "HEAD~1",
    head: str = "HEAD",
    include_uncommitted: bool = False,
) -> ReviewSource:
    """Pick the adapter implied by the configured mode."""
    if settings.mode == "github":
        return GitHubPRSource(settings)
    return LocalGitSource(
        settings,
        repo_path=repo_path,
        base=base,
        head=head,
        include_uncommitted=include_uncommitted,
    )
