from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIDEWIT_", populate_by_name=True)

    # Contexts and Tools are JSON files here (SPEC §11). One instance per directory.
    data_dir: str = "/data"
    api_key: SecretStr | None = None
    # Recent Generations kept in memory (never written to disk).
    generation_history: int = Field(default=200, ge=1, le=10000)

    llm_provider: Literal["openai", "gemini"] | None = None
    llm_model: str | None = None
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")

    @field_validator(
        "api_key", "llm_provider", "llm_model", "openai_api_key", "gemini_api_key", mode="before"
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # docker compose passes unset variables as empty strings.
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
