import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Instrument(BaseModel):
    exchange: Literal["MOEX"] = "MOEX"
    symbol: str = Field(pattern=r"^[A-Z0-9_-]{1,24}$")
    board: str = Field(default="TQBR", pattern=r"^[A-Z0-9]{1,12}$")
    keywords: list[str] = Field(default_factory=list, max_length=20)

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.board}:{self.symbol}"


def valid_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected HTTP(S) URL without credentials")
    return value


class Markets(BaseModel):
    instruments: list[Instrument] = Field(min_length=1, max_length=50)
    news_feeds: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("news_feeds")
    @classmethod
    def urls(cls, values):
        return [valid_url(v) for v in values]

    @model_validator(mode="after")
    def unique(self):
        if len({i.key for i in self.instruments}) != len(self.instruments):
            raise ValueError("Duplicate instrument")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    api_token: SecretStr
    mode: Literal["demo", "live"] = "demo"
    database_path: Path = Path("data/agent.sqlite3")
    markets_file: Path = Path("markets.example.json")
    timezone: str = "Europe/Moscow"
    schedule_enabled: bool = True
    schedule_weekday: int = Field(default=0, ge=0, le=6)
    schedule_hour: int = Field(default=9, ge=0, le=23)
    schedule_minute: int = Field(default=0, ge=0, le=59)
    schedule_catchup: bool = True
    news_lookback_days: int = Field(default=7, ge=1, le=30)
    http_timeout_seconds: float = Field(default=20, ge=1, le=60)
    http_retries: int = Field(default=2, ge=0, le=3)
    llm_enabled: bool = False
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    cors_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_config(self):
        token = self.api_token.get_secret_value()
        if len(token) < 32 or token.startswith("CHANGE_ME"):
            raise ValueError("Set API_TOKEN to a random string of at least 32 characters")
        ZoneInfo(self.timezone)
        valid_url(self.llm_base_url)
        if self.llm_enabled and (not self.llm_api_key.get_secret_value() or not self.llm_model.strip()):
            raise ValueError("LLM_ENABLED requires LLM_API_KEY and LLM_MODEL")
        if "*" in self.cors_origins:
            raise ValueError("Use explicit CORS_ORIGINS")
        return self

    def markets(self) -> Markets:
        return Markets.model_validate(json.loads(self.markets_file.read_text(encoding="utf-8")))
