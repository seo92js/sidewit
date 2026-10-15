from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIDEWIT_", populate_by_name=True, hide_input_in_errors=True
    )

    # Contexts and Tools are JSON files here (SPEC §11). One instance per directory.
    data_dir: str = "/data"
    api_key: SecretStr | None = None
    mcp_enabled: bool = False
    mcp_key: SecretStr | None = None
    mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "[::1]",
            "[::1]:*",
        ]
    )
    # No cross-origin browser access by default. Non-browser clients omit Origin.
    mcp_allowed_origins: list[str] = Field(default_factory=list)
    admin_username: str = Field(default="admin", min_length=1, max_length=128)
    admin_password: SecretStr = Field(default=SecretStr("admin"), min_length=1, max_length=1024)
    admin_cookie_secure: bool = False
    # Recent Generations kept in memory (never written to disk).
    generation_history: int = Field(default=200, ge=1, le=10000)

    llm_provider: Literal["openai", "gemini"] | None = None
    llm_model: str | None = None
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")

    @field_validator(
        "api_key",
        "mcp_key",
        "llm_provider",
        "llm_model",
        "openai_api_key",
        "gemini_api_key",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # docker compose passes unset variables as empty strings.
        return None if value == "" else value

    @field_validator("admin_username", "admin_password", mode="before")
    @classmethod
    def _admin_defaults(cls, value: object) -> object:
        return "admin" if value == "" else value

    @field_validator("admin_username")
    @classmethod
    def _admin_username(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Admin username must not contain surrounding whitespace")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
