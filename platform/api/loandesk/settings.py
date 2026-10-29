"""Platform configuration, read from ``LOANDESK_*`` environment variables (and the repo ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


def _url(name: str, port: int) -> str:
    return Field(f"http://127.0.0.1:{port}", validation_alias=AliasChoices(f"LOANDESK_{name}_URL", f"MOCK_{name}_URL"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOANDESK_", env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./loandesk.db"
    seed: int = Field(20261027, validation_alias=AliasChoices("LOANDESK_SEED", "MOCK_SEED"))
    cors_origins: list[str] = ["http://localhost:5180", "http://127.0.0.1:5180"]

    # -- the outside world (the mocks, locally on 8000-8060) ------------------------------------
    control_url: str = _url("CONTROL", 8000)
    ib_url: str = _url("IB", 8010)
    gateway_url: str = _url("GATEWAY", 8020)
    ngt_url: str = _url("NGT", 8030)
    wematch_url: str = _url("WEMATCH", 8040)
    sfp_url: str = _url("SFP", 8050)
    custodian_url: str = _url("CUSTODIAN", 8060)
    # Credentials per system. Left empty, each adapter asks the mock's /_admin/credentials (local dev only).
    mock_admin_token: str | None = Field(None, validation_alias=AliasChoices("LOANDESK_MOCK_ADMIN_TOKEN",
                                                                             "MOCK_ADMIN_TOKEN"))
    control_key: str | None = None
    ib_token: str | None = None
    gateway_token: str | None = None
    ngt_key: str | None = None
    wematch_key: str | None = None
    sfp_key: str | None = None

    # -- background sync with the venues, gateway and processor ----------------------------------
    sync_enabled: bool = True
    poll_s: float = 2.0
    availability_every_s: float = 10.0

    # -- desk policy (deterministic rules engine) -------------------------------------------------
    gc_max_fee_bps: float = 50.0
    gc_max_util_pct: float = 30.0
    hot_min_fee_bps: float = 300.0
    hot_min_util_pct: float = 85.0
    locate_name_cap: int = 1_000_000
    auto_approve_etb: bool = True  # the standing policy that clears GC/ETB locates without a human
    booking_entity: str = "LOANDESK-US"
    fat_finger_band: float = 0.5  # block fees outside last fee x (1 - band, 1 + 1/band)
    max_pct_shares_out: float = 5.0
    max_pct_adv: float = 100.0

    # -- agent (bases only for now; off means no model is ever called) -----------------------------
    agent_mode: Literal["off", "replay", "record", "live"] = "off"
    model_base_url: str = "https://api.meta.ai/v1"
    model_api_key: str | None = Field(None, validation_alias=AliasChoices("LOANDESK_MODEL_API_KEY", "META_AI_API"))
    orchestrator_model: str = "muse-wonder-1.0"
    reviewer_model: str = "muse-spark-1.3"
    parser_model: str = "muse-spark-1.3"
    budget_usd: float = 0.50
    cassette_path: Path = Path("cassettes/agent.jsonl")


@lru_cache
def get_settings() -> Settings:
    return Settings()
