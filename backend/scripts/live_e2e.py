"""Live end-to-end smoke test (requires a logged-in `claude` CLI; spends a few cents).

Exercises the real MVP loop on a throwaway repo:
  create worktree -> drive claude to make an edit -> capture diff -> cleanup.

Run:  ./.venv/bin/python scripts/live_e2e.py
Not part of the default pytest run (it needs login + network).
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.adapters import ClaudeAdapter, EventType, RunContext, TaskSpec  # noqa: E402
from app.gitops import GitOpsEngine  # noqa: E402


def sh(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cc_e2e_")
    try:
        repo = Path(tmp) / "proj"
        repo.mkdir()
        sh(["git", "init", "-b", "main"], repo)
        sh(["git", "config", "user.email", "t@example.com"], repo)
        sh(["git", "config", "user.name", "tester"], repo)
        (repo / "README.md").write_text("# demo\n")
        sh(["git", "add", "-A"], repo)
        sh(["git", "commit", "-m", "init"], repo)

        git = GitOpsEngine(repo)
        handle = git.create_worktree("demo")
        print(f"[worktree] {handle.path}  branch={handle.branch}")

        adapter = ClaudeAdapter()
        spec = TaskSpec(
            goal="Create a file named hello.txt whose only content is the single word: conductor"
        )
        ctx = RunContext(worktree_path=handle.path)

        counts: dict[str, int] = {}
        final_text = ""
        cost: dict = {}
        async for ev in adapter.run(spec, ctx):
            counts[ev.type.value] = counts.get(ev.type.value, 0) + 1
            if ev.type == EventType.tool_use:
                print(f"  [tool_use] {ev.data.get('name')}")
            elif ev.type == EventType.final:
                final_text = ev.text
            elif ev.type == EventType.cost:
                cost = ev.data
            elif ev.type == EventType.error:
                print(f"  [error/{ev.data.get('kind')}] {ev.text[:120]}")

        print(f"[events] {counts}")
        print(f"[final ] {final_text[:100]}")
        print(f"[cost  ] usd={cost.get('cost_usd')} out_tokens={cost.get('output_tokens')} "
              f"dur_ms={cost.get('duration_ms')}")

        diff = git.snapshot_and_diff(handle)
        print(f"[diff  ] files={[f.path for f in diff.files]} empty={diff.is_empty}")
        created = Path(handle.path) / "hello.txt"
        created_exists = created.exists()
        if created_exists:
            print(f"[check ] hello.txt content={created.read_text().strip()!r}")
        else:
            print("[check ] hello.txt NOT created")

        # Capture verdict before cleanup, which removes the worktree (and the file).
        ok = (not diff.is_empty) and created_exists
        git.remove_worktree(handle)
        print(f"[RESULT] {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
