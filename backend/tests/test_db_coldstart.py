"""Cold-start regression tests for init_db().

These spawn a *fresh* Python process pointed at a throwaway DB via
CC_DATABASE_URL, exactly like a real server boot: the engine is bound at import
time, create_all builds the schema, then Alembic upgrades to head. This guards
two regressions that once slipped through with no test:

  1. `from alembic import command` sat outside a try/except, so a missing
     alembic install crashed startup (ImportError). A green subprocess proves
     the dependency is present and importable.
  2. create_all + migration 001 must coexist on a fresh DB. 001 is idempotent
     (adds only missing columns) and must still stamp alembic_version='001',
     otherwise a future 002 would be skipped.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

_SNIPPET = "from app.storage.db import init_db; init_db(); print('COLDSTART_OK')"


def _cold_start(db_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **__import__("os").environ,
        "CC_DATABASE_URL": f"sqlite:///{db_path}",  # abs path -> 4 slashes
    }
    return subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )


def _columns(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _alembic_version(db_path: Path) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("SELECT version_num FROM alembic_version")]
    finally:
        con.close()


def test_cold_start_fresh_db(tmp_path):
    db = tmp_path / "fresh.db"
    proc = _cold_start(db)
    assert proc.returncode == 0, f"cold start failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    assert "COLDSTART_OK" in proc.stdout

    cols = _columns(db, "project")
    assert {"is_pinned", "is_archived", "deleted_at", "quota_tokens", "quota_cost_usd", "verify_cmd"} <= cols
    assert {"deleted_at", "error"} <= _columns(db, "task")
    assert _alembic_version(db) == ["005"]


def test_cold_start_old_schema_db(tmp_path):
    """A pre-001 DB (no new columns, no alembic_version) must migrate in place."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE project ("
        "id INTEGER PRIMARY KEY, name VARCHAR, path VARCHAR, "
        "default_branch VARCHAR, created_at DATETIME)"
    )
    con.execute(
        "INSERT INTO project (name, path, default_branch, created_at) "
        "VALUES ('legacy', '/tmp/legacy', 'main', '2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()

    assert {"is_pinned", "is_archived", "deleted_at"}.isdisjoint(_columns(db, "project"))

    proc = _cold_start(db)
    assert proc.returncode == 0, f"cold start failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"

    cols = _columns(db, "project")
    assert {"is_pinned", "is_archived", "deleted_at", "quota_tokens", "quota_cost_usd", "verify_cmd"} <= cols
    assert _alembic_version(db) == ["005"]

    con = sqlite3.connect(db)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM project")]
    finally:
        con.close()
    assert names == ["legacy"]  # existing data preserved through the migration
