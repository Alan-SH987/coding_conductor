import subprocess
from pathlib import Path

import pytest

from app import memory
from app.gitops import GitOpsEngine, NotAGitRepo


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _run(["git", "init", "-b", "main"], r)
    _run(["git", "config", "user.email", "t@example.com"], r)
    _run(["git", "config", "user.name", "tester"], r)
    (r / "README.md").write_text("hello\n")
    _run(["git", "add", "-A"], r)
    _run(["git", "commit", "-m", "init"], r)
    return r


def test_inspect_repo(repo):
    info = GitOpsEngine(repo).inspect_repo()
    assert info.is_git
    assert info.default_branch == "main"
    assert info.is_dirty is False
    assert info.head_sha


def test_not_a_git_repo(tmp_path):
    with pytest.raises(NotAGitRepo):
        GitOpsEngine(tmp_path).inspect_repo()


def test_create_and_remove_worktree(repo):
    e = GitOpsEngine(repo)
    h = e.create_worktree("1")
    assert Path(h.path).exists()
    assert h.branch == "conductor/task-1"
    assert Path(h.path).resolve() != repo.resolve()  # isolated, outside main repo

    e.remove_worktree(h)
    assert not Path(h.path).exists()
    branches = subprocess.run(
        ["git", "branch", "--list", h.branch], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout.strip()
    assert branches == ""  # branch cleaned up


def test_dirty_main_allows_worktree_but_blocks_merge(repo):
    """Moved guard: a dirty main no longer blocks worktree creation (the worktree
    is isolated); the clean-main requirement now lives at merge_to (approve)."""
    e = GitOpsEngine(repo)
    h = e.create_worktree("1")  # clean main: fine

    # dirty the main work tree with an uncommitted tracked change
    (repo / "README.md").write_text("uncommitted change\n")
    assert e.inspect_repo().is_dirty is True

    # create_worktree still succeeds on a dirty main (isolated from the work tree)
    h2 = e.create_worktree("2")
    assert Path(h2.path).exists()
    e.remove_worktree(h2)

    # but merge_to refuses and surfaces the offending files
    res = e.merge_to(h)
    assert res.ok is False and res.dirty is True
    assert "README.md" in res.dirty_files


def test_first_run_scaffold_stays_clean(repo):
    """回归：在已提交 .gitignore 的仓库上首次 scaffold 不应弄脏工作区。

    旧 bug：ensure_conductor 往受版本控制的 .gitignore 追加忽略规则，使主仓库变 dirty，
    首个 Run 的 create_worktree 直接抛 DirtyRepoError（0 runs）。修复后忽略规则写入
    .git/info/exclude（不被跟踪），工作区保持干净。
    """
    # A pre-committed .gitignore that lacks Conductor's patterns (the trigger case).
    (repo / ".gitignore").write_text("__pycache__/\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "add gitignore"], repo)

    memory.ensure_conductor(repo)

    # The tracked .gitignore must be untouched -> work tree stays clean.
    assert (repo / ".gitignore").read_text() == "__pycache__/\n"
    assert GitOpsEngine(repo).inspect_repo().is_dirty is False

    # Patterns landed in the per-repo, untracked exclude file instead.
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert ".conductor/diffs/" in exclude
    assert ".cc-worktrees/" in exclude

    # And the very first Run can now branch off a clean main without raising.
    h = GitOpsEngine(repo).create_worktree("1")
    assert Path(h.path).exists()
    GitOpsEngine(repo).remove_worktree(h)


def test_empty_diff(repo):
    e = GitOpsEngine(repo)
    h = e.create_worktree("7")
    diff = e.snapshot_and_diff(h)
    assert diff.is_empty
    assert diff.files == []
    e.remove_worktree(h)


def test_full_cycle_diff_and_merge(repo):
    """切 worktree -> agent 改文件 -> 拿 diff -> 合并 -> 主分支生效，主工作区合并前零污染。"""
    e = GitOpsEngine(repo)
    h = e.create_worktree("42")

    # Simulate an agent editing inside the isolated worktree.
    (Path(h.path) / "feature.py").write_text("print('hello from agent')\n")

    diff = e.snapshot_and_diff(h)
    assert not diff.is_empty
    paths = {f.path for f in diff.files}
    assert "feature.py" in paths
    added = next(f for f in diff.files if f.path == "feature.py")
    assert added.additions == 1

    # Before merge: main working tree is untouched.
    assert not (repo / "feature.py").exists()

    res = e.merge_to(h)
    assert res.ok
    assert res.conflict is False
    assert res.merged_sha

    # After merge: change is now on the main branch.
    assert (repo / "feature.py").exists()

    e.remove_worktree(h)
    assert not Path(h.path).exists()


def test_link_deps_into_worktree_links_only_ignored_dirs(repo):
    """link_deps symlinks git-ignored dep dirs into the worktree (so a verify can
    run there), and skips non-ignored ones so the symlink can't leak into a diff."""
    e = GitOpsEngine(repo)
    (repo / ".gitignore").write_text("node_modules/\n")  # node_modules ignored; venv NOT
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "ignore node_modules"], repo)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "pkg.txt").write_text("dep\n")
    (repo / "venv").mkdir()
    (repo / "venv" / "x.txt").write_text("y\n")

    h = e.create_worktree("1")
    linked = e.link_deps_into_worktree(h.path)

    assert linked == ["node_modules"]  # only the ignored dep dir
    nm = Path(h.path) / "node_modules"
    assert nm.is_symlink() and (nm / "pkg.txt").exists()  # resolves to main's deps
    assert not (Path(h.path) / "venv").exists()  # non-ignored skipped (no diff leak)
