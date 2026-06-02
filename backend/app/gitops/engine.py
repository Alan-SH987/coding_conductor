"""GitOps Engine — worktree isolation, diff capture, gated merge.

Hard rule: an agent never touches the main branch directly. Each task runs in
its own git worktree on a dedicated `conductor/task-<id>` branch; changes are
captured as a diff and only merged after human approval.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .errors import (
    GitCommandError,
    NotAGitRepo,
    WorktreeExistsError,
)
from .models import DiffFile, DiffResult, MergeResult, RepoInfo, WorktreeHandle

CONDUCTOR_NAME = "Coding Conductor"
CONDUCTOR_EMAIL = "conductor@local"
BRANCH_PREFIX = "conductor/task-"


class GitOpsEngine:
    def __init__(self, repo_path: str | Path, worktrees_dirname: str = ".cc-worktrees"):
        self.repo_path = Path(repo_path).resolve()
        # Worktrees live OUTSIDE the repo (sibling dir) so they are never tracked.
        self.worktrees_root = self.repo_path.parent / worktrees_dirname / self.repo_path.name

    # ----- low level -----------------------------------------------------
    def _git(self, args: list[str], cwd: Optional[str | Path] = None,
             check: bool = True) -> subprocess.CompletedProcess:
        target = Path(cwd) if cwd else self.repo_path
        proc = subprocess.run(
            ["git", *args], cwd=str(target), capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            raise GitCommandError(args, proc.returncode, proc.stderr)
        return proc

    def _as_conductor(self, args: list[str]) -> list[str]:
        # Carry an identity so commits/merges work even if the repo has no git user.
        return [
            "-c", f"user.name={CONDUCTOR_NAME}",
            "-c", f"user.email={CONDUCTOR_EMAIL}",
            *args,
        ]

    # ----- inspection ----------------------------------------------------
    def inspect_repo(self) -> RepoInfo:
        probe = self._git(["rev-parse", "--is-inside-work-tree"], check=False)
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            raise NotAGitRepo(f"{self.repo_path} is not a git work tree")
        branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        # Untracked files (e.g. a fresh .conductor/) must not block worktree
        # creation; only uncommitted changes to tracked files count as "dirty".
        is_dirty = bool(
            self._git(["status", "--porcelain", "--untracked-files=no"]).stdout.strip()
        )
        head_sha = self._git(["rev-parse", "HEAD"]).stdout.strip()
        return RepoInfo(is_git=True, default_branch=branch, is_dirty=is_dirty, head_sha=head_sha)

    def init_repo(self, default_branch: str = "main") -> RepoInfo:
        """Initialize a brand-new repo at repo_path (for adding an empty project).

        Creates the directory if needed, runs ``git init``, and makes an initial
        commit (empty if the dir has no files) so HEAD exists — create_worktree
        needs a base commit to branch from. Only call on a path that is not
        already a git work tree.
        """
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self._git(["init", "-b", default_branch])
        self._git(["add", "-A"])
        self._git(self._as_conductor(
            ["commit", "--allow-empty", "-m", "initial commit (conductor)"]
        ))
        return self.inspect_repo()

    # ----- worktree lifecycle -------------------------------------------
    def _branch_for(self, task_id: str | int) -> str:
        return f"{BRANCH_PREFIX}{task_id}"

    def _path_for(self, task_id: str | int) -> Path:
        return self.worktrees_root / f"task-{task_id}"

    def create_worktree(self, task_id: str | int, base: Optional[str] = None) -> WorktreeHandle:
        # A worktree is checked out from a COMMIT (base_sha) into an isolated
        # directory, so a dirty main is harmless here — the human's uncommitted
        # changes are never touched. The clean-main requirement lives at merge_to
        # (approve), where git would actually clobber/conflict with them.
        info = self.inspect_repo()
        base = base or info.default_branch
        base_sha = self._git(["rev-parse", base]).stdout.strip()
        wt_path = self._path_for(task_id)
        if wt_path.exists():
            raise WorktreeExistsError(f"worktree path already exists: {wt_path}")
        branch = self._branch_for(task_id)
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(["worktree", "add", "-b", branch, str(wt_path), base_sha])
        return WorktreeHandle(
            task_id=str(task_id),
            path=str(wt_path),
            branch=branch,
            base_sha=base_sha,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def reset_worktree(self, task_id: str | int) -> None:
        """Drop any existing worktree/branch for this task id (idempotent).

        A failed run leaves its worktree and ``conductor/task-<id>`` branch on
        disk (only approve/reject clean them up), so a retry must clear the stale
        attempt before ``create_worktree`` — otherwise it collides. Best-effort:
        a no-op when nothing exists, so it is safe to call before every run.
        """
        self.remove_worktree(
            WorktreeHandle(
                task_id=str(task_id),
                path=str(self._path_for(task_id)),
                branch=self._branch_for(task_id),
                base_sha="",
                created_at="",
            )
        )

    def merge_base(self, branch: str, target: Optional[str] = None) -> str:
        """Best common ancestor of ``branch`` and ``target`` (the default branch).

        Restores a reused worktree's diff base when revising: the range
        ``merge_base..HEAD`` still shows the whole task change set even after
        extra revision commits land on the branch.
        """
        info = self.inspect_repo()
        target = target or info.default_branch
        return self._git(["merge-base", branch, target]).stdout.strip()

    def snapshot_and_diff(self, handle: WorktreeHandle) -> DiffResult:
        wt = handle.path
        self._git(["add", "-A"], cwd=wt)
        staged = self._git(["diff", "--cached", "--name-only"], cwd=wt).stdout.strip()
        if staged:
            self._git(
                self._as_conductor(["commit", "-m", f"conductor: task-{handle.task_id}"]),
                cwd=wt,
            )
        rng = f"{handle.base_sha}..HEAD"
        unified = self._git(["diff", rng], cwd=wt).stdout
        if not unified.strip():
            return DiffResult(files=[], unified_diff="", stat="", is_empty=True)
        stat = self._git(["diff", "--stat", rng], cwd=wt).stdout
        files = self._parse_files(wt, rng)
        return DiffResult(files=files, unified_diff=unified, stat=stat, is_empty=False)

    def _parse_files(self, wt: str, rng: str) -> list[DiffFile]:
        numstat = self._git(["diff", "--numstat", rng], cwd=wt).stdout.strip().splitlines()
        namestat = self._git(["diff", "--name-status", rng], cwd=wt).stdout.strip().splitlines()
        status_by_path: dict[str, str] = {}
        for line in namestat:
            parts = line.split("\t")
            status_by_path[parts[-1]] = parts[0]
        files: list[DiffFile] = []
        for line in numstat:
            add, dele, path = line.split("\t")
            files.append(
                DiffFile(
                    path=path,
                    status=status_by_path.get(path, "M"),
                    additions=0 if add == "-" else int(add),
                    deletions=0 if dele == "-" else int(dele),
                )
            )
        return files

    def _dirty_files(self) -> list[str]:
        """Tracked files with uncommitted changes (porcelain paths).

        Don't strip the whole output first — a modified-unstaged line starts with
        a space (" M README.md"), and a global strip would shift every path by a
        char. Split first, then slice off the 2-char status + space.
        """
        out = self._git(["status", "--porcelain", "--untracked-files=no"]).stdout
        return [line[3:] for line in out.splitlines() if line]

    def merge_to(self, handle: WorktreeHandle, target: Optional[str] = None,
                 strategy: str = "no-ff", verify_cmd: Optional[str] = None) -> MergeResult:
        info = self.inspect_repo()
        # Clean-main guard (moved here from create_worktree): a worktree is
        # isolated, but merging into main would clobber/conflict with uncommitted
        # changes. Block with a clear signal instead of attempting the merge.
        if info.is_dirty:
            return MergeResult(
                ok=False, merged_sha=None, conflict=False,
                dirty=True, dirty_files=self._dirty_files(),
            )
        target = target or info.default_branch
        # Check if the branch to merge actually exists. If not, the task was
        # likely already merged (branch deleted) — return success for idempotency.
        # This handles race conditions where two merge requests arrive nearly
        # simultaneously, or the user clicks "merge" again before the page refreshes.
        branch_check = self._git(["rev-parse", "--verify", handle.branch], check=False)
        if branch_check.returncode != 0:
            # Branch doesn't exist — treat as already merged (idempotent success)
            return MergeResult(ok=True, merged_sha=None, conflict=False, push_ok=True)
        if info.default_branch != target:
            self._git(["checkout", target])
        flag = "--no-ff" if strategy == "no-ff" else "--ff"
        # Stage the merge but DON'T commit yet, so an optional verify command can
        # gate it: a failing build/test aborts cleanly, leaving no commit on main.
        proc = self._git(
            self._as_conductor(["merge", "--no-commit", flag, handle.branch]),
            check=False,
        )
        conflicted = (
            self._git(["diff", "--name-only", "--diff-filter=U"], check=False)
            .stdout.strip()
            .splitlines()
        )
        if conflicted:
            self._git(["merge", "--abort"], check=False)
            return MergeResult(ok=False, merged_sha=None, conflict=True, conflicted_files=conflicted)
        # MERGE_HEAD is set while a --no-commit merge is pending. Absent + rc!=0
        # means the merge never started (e.g. dirty tree); absent + rc==0 means
        # the branch was already up to date (a no-op, nothing to commit).
        merging = self._git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False).returncode == 0
        if not merging and proc.returncode != 0:
            return MergeResult(ok=False, merged_sha=None, conflict=False, verify_output=proc.stderr.strip())
        if verify_cmd and merging:
            ok, output = self._run_verify(verify_cmd)
            if not ok:
                self._git(["merge", "--abort"], check=False)
                return MergeResult(
                    ok=False, merged_sha=None, conflict=False,
                    verify_failed=True, verify_output=output,
                )
        if merging:
            self._git(
                self._as_conductor(["commit", "-m", f"conductor: merge task-{handle.task_id}"]),
                check=False,
            )
        merged_sha = self._git(["rev-parse", "HEAD"]).stdout.strip()
        # Push to remote after successful merge
        push_ok, push_output = self._push_to_remote(target)
        return MergeResult(ok=True, merged_sha=merged_sha, conflict=False,
                          push_ok=push_ok, push_output=push_output)

    def _push_to_remote(self, branch: str, remote: str = "origin") -> tuple[bool, str]:
        """Push the merged branch to remote (best-effort, non-blocking).

        Returns (ok, output) where ok is True if push succeeded or no remote
        exists. A missing remote is not an error — local-only repos are valid.
        """
        # Check if remote exists
        probe = self._git(["remote", "get-url", remote], check=False)
        if probe.returncode != 0:
            # No remote configured, that's fine
            return True, "no remote configured"
        # Push to remote
        proc = self._git(["push", remote, branch], check=False)
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > 4000:
            out = out[:4000] + "…(truncated)"
        return proc.returncode == 0, out.strip()

    def _run_verify(self, cmd: str, timeout: int = 600) -> tuple[bool, str]:
        """Run an operator-configured verify command in the main repo working dir.

        It runs HERE (not in a worktree) because that is the only checkout with
        dependencies installed — a fresh worktree has no node_modules/.venv, so a
        build/test would spuriously fail. ``shell=True`` so compound commands like
        ``cd frontend && npm run build`` work. The command is configured by the
        repo operator (trusted, CI-script equivalent), not from untrusted input.
        """
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(self.repo_path),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"verify command timed out after {timeout}s: {cmd}"
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > 16000:
            out = "…(truncated)…\n" + out[-16000:]
        return proc.returncode == 0, out.strip()

    def remove_worktree(self, handle: WorktreeHandle, force: bool = True) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(handle.path)
        self._git(args, check=False)
        self._git(["branch", "-D", handle.branch], check=False)
        self._git(["worktree", "prune"], check=False)

    def list_worktrees(self) -> list[dict]:
        # MVP: return raw porcelain records (path/sha/branch) for ops/recovery.
        out = self._git(["worktree", "list", "--porcelain"]).stdout
        records: list[dict] = []
        cur: dict = {}
        for line in out.splitlines():
            if line.startswith("worktree "):
                cur = {"path": line[len("worktree "):]}
            elif line.startswith("HEAD "):
                cur["sha"] = line[len("HEAD "):]
            elif line.startswith("branch "):
                cur["branch"] = line[len("branch "):]
            elif line == "":
                if cur:
                    records.append(cur)
                    cur = {}
        if cur:
            records.append(cur)
        return records
