import logging
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    # Import models so they register on SQLModel.metadata before create_all.
    from app.storage import models  # noqa: F401

    # For new databases, create_all sets up the schema.
    SQLModel.metadata.create_all(engine)

    # Run Alembic migrations for existing databases that need schema updates.
    _run_migrations()


def _run_migrations() -> None:
    """Run pending Alembic migrations."""
    from alembic import command
    from alembic.config import Config

    backend_dir = Path(__file__).parent.parent.parent
    alembic_cfg = Config(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)

    try:
        command.upgrade(alembic_cfg, "head")
    except Exception as e:
        logger.warning("Alembic migration skipped or failed: %s", e)


def get_session():
    with Session(engine) as session:
        yield session
