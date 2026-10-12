"""
FS-CAE Configuration Module

Typed configuration using pydantic-settings.
Validates environment variables and enforces production constraints.
"""

from __future__ import annotations

from typing import List, Optional, Union

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    DATABASE_URL: str = "sqlite:///./foundry_cae.db"

    # --- Timezone ---
    TIMEZONE: str = "Africa/Lusaka"

    # --- Environment ---
    ENVIRONMENT: str = "development"

    # --- Legal Entity ---
    FOUNDRY_LEGAL_NAME: str = "Foundry Systems (Elysium Group Limited)"

    # --- Banking Details (required in production) ---
    FOUNDRY_BANK_NAME: Optional[str] = None
    FOUNDRY_ACCOUNT_NUMBER: Optional[str] = None
    FOUNDRY_BRANCH_CODE: Optional[str] = None

    # --- Gemini API Keys ---
    GEMINI_API_KEYS: Union[List[str], str] = []
    GEMINI_API_KEY_1: Optional[str] = None
    GEMINI_API_KEY_2: Optional[str] = None
    GEMINI_API_KEY_3: Optional[str] = None

    # --- Layer 2: Gemini Intelligence ---
    GEMINI_MODEL: str = "gemini-1.5-flash"
    GEMINI_RPM_PER_KEY: int = 10        # Max requests per minute per key
    GEMINI_COOLDOWN_SECONDS: int = 60   # Cooldown on 429 / quota errors

    # --- Layer 2: Ingestion ---
    DATA_INPUT_DIR: str = "data/input"
    QUALIFICATION_THRESHOLD: int = 65   # Minimum fit_score to qualify

    # --- Layer 3: Outreach & Dispatcher ---
    DAILY_DISPATCH_CAP: int = 100
    DISPATCH_MIN_DELAY_SECONDS: float = 60.0
    DISPATCH_MAX_DELAY_SECONDS: float = 120.0
    OPERATING_HOURS_START: str = "08:00"
    OPERATING_HOURS_END: str = "16:30"
    EMAIL_TEST_MODE: bool = True
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: Optional[str] = None

    # ----- Validators -----

    @field_validator("ENVIRONMENT")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "testing", "production"}
        if v not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {allowed}, got '{v}'")
        return v

    @field_validator("GEMINI_API_KEYS", mode="before")
    @classmethod
    def parse_gemini_keys(cls, v):
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        return v or []

    @model_validator(mode="after")
    def merge_keys_and_enforce_production(self) -> "Settings":
        # Auto-merge indexed keys if present
        extra_keys = [
            k.strip() for k in [self.GEMINI_API_KEY_1, self.GEMINI_API_KEY_2, self.GEMINI_API_KEY_3]
            if k and k.strip()
        ]
        if extra_keys:
            merged = list(self.GEMINI_API_KEYS)
            for k in extra_keys:
                if k not in merged:
                    merged.append(k)
            self.GEMINI_API_KEYS = merged

        if self.ENVIRONMENT == "production":
            missing = []
            if not self.FOUNDRY_BANK_NAME:
                missing.append("FOUNDRY_BANK_NAME")
            if not self.FOUNDRY_ACCOUNT_NUMBER:
                missing.append("FOUNDRY_ACCOUNT_NUMBER")
            if not self.FOUNDRY_BRANCH_CODE:
                missing.append("FOUNDRY_BRANCH_CODE")
            if missing:
                raise ValueError(
                    f"Production environment requires: {', '.join(missing)}"
                )
        return self


def get_settings(**overrides) -> Settings:
    """Factory function for settings, allowing test overrides."""
    return Settings(**overrides)
