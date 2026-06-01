from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CC_", env_file=".env", extra="ignore")

    app_name: str = "Coding Conductor"
    database_url: str = "sqlite:///./conductor.db"
    # Directory name (sibling to each managed repo) that holds task worktrees.
    worktrees_dirname: str = ".cc-worktrees"


settings = Settings()
