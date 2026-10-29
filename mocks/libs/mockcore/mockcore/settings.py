from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    """Settings every mock reads from the environment (prefix ``MOCK_``)."""

    model_config = SettingsConfigDict(env_prefix="MOCK_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres@localhost:5432/loandesk_mocks"
    seed: int = 20261027
    # How often mock-control re-reads its clock row (wall seconds).
    clock_poll_s: float = 0.25
    # How often mock-control re-pushes the clock to every mock, so a restarted one catches up.
    clock_push_s: float = 5.0
    # How often the scheduler checks for due jobs in live mode (wall seconds).
    scheduler_poll_s: float = 0.05
    # Service URLs, used by mock-control for fan-out and by services that call each other.
    control_url: str = "http://localhost:8000"
    ib_url: str = "http://localhost:8010"
    gateway_url: str = "http://localhost:8020"
    ngt_url: str = "http://localhost:8030"
    wematch_url: str = "http://localhost:8040"
    sfp_url: str = "http://localhost:8050"
    custodian_url: str = "http://localhost:8060"
    analytics_url: str = "http://localhost:8070"
    # Shared admin token for /_admin routes. Empty disables the check (local dev).
    admin_token: str = ""


@lru_cache
def settings() -> CoreSettings:
    return CoreSettings()
