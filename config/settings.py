"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

import enum
import functools
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMode(str, enum.Enum):
    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"


class ProductType(str, enum.Enum):
    NRML = "NRML"
    MIS = "MIS"


class Settings(BaseSettings):
    """All configuration is read from environment variables or a .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Telegram ──
    telegram_api_id: int = Field(..., description="Telegram API ID from my.telegram.org")
    telegram_api_hash: str = Field(..., description="Telegram API hash from my.telegram.org")
    telegram_group_id: int | str = Field(
        ...,
        description="Telegram group chat ID (numeric) or username",
    )

    # ── Groww ──
    groww_api_token: str = Field(..., description="Groww Trade API auth token")

    # ── App mode ──
    mode: AppMode = Field(default=AppMode.PAPER, description="Application mode")

    # ── Trading ──
    default_lot_multiplier: int = Field(
        default=1,
        ge=1,
        description="Number of lots per signal",
    )
    max_risk_per_trade: float = Field(
        default=5000.0,
        gt=0,
        description="Maximum risk per trade in INR",
    )
    default_product: ProductType = Field(
        default=ProductType.NRML,
        description="Default product type for orders",
    )

    # ── Database ──
    database_path: str = Field(
        default="data/automa.db",
        description="Path to SQLite database file",
    )

    # ── Logging ──
    log_level: str = Field(default="INFO", description="Logging level")

    # ── Derived / internal ──
    session_path: str = Field(
        default="data/automa_session",
        description="Path for Telethon session file",
    )

    @field_validator("telegram_group_id", mode="before")
    @classmethod
    def _coerce_group_id(cls, v: str | int) -> int | str:
        """Accept both numeric IDs and string usernames."""
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return v  # keep as username string
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    def ensure_data_dir(self) -> None:
        """Create the data directory if it does not exist."""
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.session_path).parent.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    settings = Settings()  # type: ignore[call-arg]
    settings.ensure_data_dir()
    return settings
