from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENCRYPTION_KEY: str = ""
    JWT_SECRET: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24
    DATABASE_URL: str = "sqlite:///./chatbot.db"
    FRONTEND_ORIGIN: str = "http://localhost:5000"
    UPLOAD_DIR: str = "./uploads"

    # upload limits
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024
    MAX_UPLOADS_PER_TURN: int = 6
    ALLOWED_IMAGE_TYPES: tuple[str, ...] = (
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    )
    ALLOWED_DOC_TYPES: tuple[str, ...] = (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
        "text/plain",
        "text/markdown",
        "application/octet-stream",
    )
    MAX_DOC_BYTES: int = 25 * 1024 * 1024

    # generated-file retention (days); files older than this are pruned on startup
    GENERATED_RETENTION_DAYS: int = 14

    # agent loop
    MAX_TOOL_ITERS: int = 10
    MAX_TOOL_CONCURRENCY: int = 10
    # Hard cap on file-generating tool calls (generate_*) per lane run, so a looping
    # model can't produce dozens of duplicate files.
    MAX_GENERATE_CALLS: int = 6

    # LLM generation
    LLM_MAX_TOKENS: int = 4096
    LLM_REQUEST_TIMEOUT: float = 120.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
