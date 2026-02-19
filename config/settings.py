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


class OrderPlacement(str, enum.Enum):
    """How to place entry/exit orders.

    LIMIT  – place at the signal price (may not fill if price moves away)
    MARKET – place at market price (guaranteed fill, but price may differ)
    """
    LIMIT = "LIMIT"
    MARKET = "MARKET"


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
    groww_api_token: str = Field(..., description="Groww Trade API key (JWT)")
    groww_api_secret: str = Field(
        default="",
        description="Groww Trade API secret (used with API-key/secret flow, requires daily approval)",
    )

    # TOTP-based auth (no daily approval needed)
    groww_totp_token: str = Field(
        default="",
        description="Groww TOTP token (from API Keys page → Generate TOTP token)",
    )
    groww_totp_secret: str = Field(
        default="",
        description="Groww TOTP secret (used to generate time-based OTP codes)",
    )

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
    entry_order_type: OrderPlacement = Field(
        default=OrderPlacement.MARKET,
        description="Order type for entries: LIMIT (signal price) or MARKET (current price)",
    )
    exit_order_type: OrderPlacement = Field(
        default=OrderPlacement.MARKET,
        description="Order type for exits: LIMIT (signal price) or MARKET (current price)",
    )
    default_sl_percent: float = Field(
        default=30.0,
        gt=0,
        le=100,
        description="Default stoploss as % below entry price, used when the signal has no SL",
    )

    # ── Database ──
    database_path: str = Field(
        default="data/automa.db",
        description="Path to SQLite database file",
    )

    # ── Logging ──
    log_level: str = Field(default="INFO", description="Logging level")

    # ── Daily Summary Email ──
    daily_summary_enabled: bool = Field(
        default=True,
        description="Send daily P&L summary email at market close",
    )
    daily_summary_time: str = Field(
        default="15:30",
        description="Time to send daily summary (HH:MM in IST, e.g. 15:30)",
    )
    summary_email_to: str = Field(
        default="",
        description="Recipient email address for daily summary",
    )
    smtp_host: str = Field(
        default="smtp.gmail.com",
        description="SMTP server hostname",
    )
    smtp_port: int = Field(
        default=587,
        description="SMTP server port (587 for TLS, 465 for SSL)",
    )
    smtp_user: str = Field(
        default="",
        description="SMTP username (email address for Gmail)",
    )
    smtp_password: str = Field(
        default="",
        description="SMTP password or app-specific password",
    )

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
