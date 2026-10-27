"""25 fictional counterparties with venue codes, fake-prefix LEIs, limits and legal terms."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache

from mockcore import rng
from mockcore.auth import derive_secret

from . import identifiers as ids


@dataclass(frozen=True)
class Counterparty:
    code: str  # our code
    name: str
    kind: str  # PRIME_BROKER | AGENT_LENDER | HEDGE_FUND | BROKER_DEALER | INTERNAL
    region: str
    ngt_code: str
    wematch_entity: str
    lei: str
    bic: str
    credit_limit_usd: int
    agreement: str  # GMSLA_2010 | GMSLA_2018 | MSLA
    collateral_schedule: str
    persona: str | None  # chat persona handle in mock-ib, if any
    api_key: str
    webhook_secret: str

    def to_json(self, include_secrets: bool = False) -> dict:
        d = asdict(self)
        if not include_secrets:
            d.pop("api_key")
            d.pop("webhook_secret")
        return d


# (code, name, kind, region, persona)
_ROSTER: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("PBALPHA", "Alpha Prime Services", "PRIME_BROKER", "US", "pb-alpha"),
    ("PBBRAVO", "Bravo Prime Brokerage", "PRIME_BROKER", "UK", "pb-bravo"),
    ("HARBORAL", "Harbor Lane Agency Lending", "AGENT_LENDER", "US", "harbor-lending"),
    ("KESTRELHF", "Kestrel Point Capital", "HEDGE_FUND", "US", "kestrel-hf"),
    ("DELTAONE", "Internal: Delta One Desk", "INTERNAL", "US", "delta-one"),
    ("JUNIPERHF", "Juniper Ridge Partners", "HEDGE_FUND", "UK", "juniper-hf"),
    ("PBCHARLIE", "Charlie Prime Finance", "PRIME_BROKER", "US", None),
    ("PBECHO", "Echo Securities Prime", "PRIME_BROKER", "EU", None),
    ("NORTHAL", "Northgate Custody Lending", "AGENT_LENDER", "US", None),
    ("MERIDIAL", "Meridian Trust Securities Lending", "AGENT_LENDER", "UK", None),
    ("SAKURAAL", "Sakura Custody Services", "AGENT_LENDER", "JP", None),
    ("RHINEAL", "Rhine Asset Servicing", "AGENT_LENDER", "EU", None),
    ("ORIONBD", "Orion Markets LLC", "BROKER_DEALER", "US", None),
    ("TALBOTBD", "Talbot & Finch Securities", "BROKER_DEALER", "UK", None),
    ("LUMENBD", "Lumen Capital Markets", "BROKER_DEALER", "EU", None),
    ("CEDARHF", "Cedar Hollow Fund LP", "HEDGE_FUND", "US", None),
    ("BASALTHF", "Basalt Long/Short Master Fund", "HEDGE_FUND", "US", None),
    ("ORCHIDHF", "Orchid Bay Capital", "HEDGE_FUND", "JP", None),
    ("SPRUCEHF", "Spruce Arbitrage Partners", "HEDGE_FUND", "UK", None),
    ("PENSIONA", "Granite State Pension Plan", "AGENT_LENDER", "US", None),
    ("SOVWEALTH", "Northern Sovereign Reserve Fund", "AGENT_LENDER", "EU", None),
    ("MUTUALX", "Riverbend Mutual Funds", "AGENT_LENDER", "US", None),
    ("INSUREY", "Beacon Life Assurance", "AGENT_LENDER", "UK", None),
    ("DELTATWO", "Internal: Equity Finance Desk", "INTERNAL", "UK", None),
    ("TREASINT", "Internal: Treasury Funding", "INTERNAL", "US", None),
)

_COLLATERAL = {
    "US": "CASH_USD_102 | US_TREASURIES_102",
    "UK": "CASH_GBP_105 | G10_GOVTS_105 | MAJOR_INDEX_EQUITY_110",
    "EU": "CASH_EUR_105 | EU_GOVTS_105",
    "JP": "CASH_JPY_105 | JGB_105",
}


@lru_cache(maxsize=4)
def _build(seed: int) -> tuple[Counterparty, ...]:
    r = rng.fresh("refdata", "counterparties", seed=seed)
    out = []
    for i, (code, name, kind, region, persona) in enumerate(_ROSTER, start=1):
        lei_body = "MOCK00" + "".join(r.choice(ids.ALNUM) for _ in range(12))
        agreement = "MSLA" if region == "US" else r.choice(("GMSLA_2010", "GMSLA_2018"))
        limit = {"INTERNAL": 5_000_000_000, "AGENT_LENDER": 3_000_000_000, "PRIME_BROKER": 2_000_000_000,
                 "BROKER_DEALER": 1_000_000_000, "HEDGE_FUND": 400_000_000}[kind]
        out.append(
            Counterparty(
                code=code,
                name=name,
                kind=kind,
                region=region,
                ngt_code=f"{4000 + i * 37:04d}",
                wematch_entity=f"WM-{code}-{ {'US': 'NYC', 'UK': 'LDN', 'EU': 'FRA', 'JP': 'TYO'}[region] }",
                lei=ids.make_lei(lei_body),
                bic=f"MK{code[:2]}{ {'US': 'US', 'UK': 'GB', 'EU': 'DE', 'JP': 'JP'}[region] }33XXX",
                credit_limit_usd=int(limit * r.uniform(0.6, 1.4)) // 1_000_000 * 1_000_000,
                agreement=agreement,
                collateral_schedule=_COLLATERAL[region],
                persona=persona,
                api_key="mk_" + derive_secret(seed, "api", code)[:24],
                webhook_secret=derive_secret(seed, "webhook", code),
            )
        )
    return tuple(out)


def counterparties(seed: int | None = None) -> tuple[Counterparty, ...]:
    return _build(rng.get_seed() if seed is None else seed)


def counterparty_by_code(code: str, seed: int | None = None) -> Counterparty | None:
    code = code.upper()
    for cp in counterparties(seed):
        if code in (cp.code, cp.ngt_code, cp.wematch_entity, cp.lei):
            return cp
    return None


def counterparty_by_api_key(key: str, seed: int | None = None) -> Counterparty | None:
    for cp in counterparties(seed):
        if cp.api_key == key:
            return cp
    return None


# Our own firm, as seen by the venues and the processor.
OUR_FIRM = {
    "code": "LOANDESK",
    "name": "LoanDesk Demo Bank",
    "ngt_code": "0001",
    "wematch_entity": "WM-LOANDESK-NYC",
    "lei": ids.make_lei("MOCK00LOANDESK0001"),
    "bic": "MKLDUS33XXX",
}
