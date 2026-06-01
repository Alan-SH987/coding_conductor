import subprocess
from pathlib import Path

import pytest

from app.gitops import DirtyRepoError, GitOpsEngine, NotAGitRepo


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


def test_dirty_repo_rejected(repo):
    (repo / "README.md").write_text("uncommitted change\n")
    with pytest.raises(DirtyRepoError):
        GitOpsEngine(repo).create_worktree("1")


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
