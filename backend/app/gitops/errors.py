class GitOpsError(Exception):
    """Base class for all GitOps failures."""


class NotAGitRepo(GitOpsError):
    """The given path is not inside a git work tree."""


class DirtyRepoError(GitOpsError):
    """The main repo has uncommitted changes; refusing to proceed."""


class WorktreeExistsError(GitOpsError):
    """A worktree path for this task already exists."""


class GitCommandError(GitOpsError):
    """A git subprocess exited non-zero."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(cmd)} failed ({returncode}): {stderr.strip()}")
