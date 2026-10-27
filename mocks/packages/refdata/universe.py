"""2,000 fictional equities across US/UK/EU/JP with check-digit-correct fictitious identifiers."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from functools import lru_cache

from mockcore import rng

from . import identifiers as ids

# Tier bands (spec defaults): GC under 50 bps and util under 30%; Hot above 300 bps or util above 85%.
GC_MAX_FEE, GC_MAX_UTIL = 50.0, 30.0
HOT_MIN_FEE, HOT_MIN_UTIL = 300.0, 85.0


def classify(fee_bps: float, util_pct: float) -> str:
    if fee_bps > HOT_MIN_FEE or util_pct > HOT_MIN_UTIL:
        return "HOT"
    if fee_bps < GC_MAX_FEE and util_pct < GC_MAX_UTIL:
        return "GC"
    return "WARM"


@dataclass(frozen=True)
class Market:
    country: str
    ccy: str
    mics: tuple[str, ...]
    exch_code: str  # composite suffix used in "TICK XX" style tickers
    index: str
    count: int
    ticker_style: str  # "alpha" or "numeric"
    px_median: float


MARKETS: tuple[Market, ...] = (
    Market("US", "USD", ("XNYS", "XNAS"), "US", "MOCK US 500", 900, "alpha", 45.0),
    Market("GB", "GBP", ("XLON",), "LN", "MOCK UK 100", 400, "alpha", 6.0),
    Market("DE", "EUR", ("XETR",), "GY", "MOCK EU 50", 140, "alpha", 40.0),
    Market("FR", "EUR", ("XPAR",), "FP", "MOCK EU 50", 140, "alpha", 40.0),
    Market("NL", "EUR", ("XAMS",), "NA", "MOCK EU 50", 120, "alpha", 30.0),
    Market("JP", "JPY", ("XTKS",), "JT", "MOCK JP 225", 300, "numeric", 2200.0),
)

SECTORS = (
    "Information Technology",
    "Health Care",
    "Financials",
    "Consumer Discretionary",
    "Communication Services",
    "Industrials",
    "Consumer Staples",
    "Energy",
    "Utilities",
    "Real Estate",
    "Materials",
)

_NAME_A = (
    "Arbor", "Brightwater", "Cobalt", "Driftwood", "Ember", "Fernvale", "Granite", "Halcyon", "Ironleaf",
    "Juniper", "Kestrel", "Larkspur", "Meridian", "Northwind", "Oakhurst", "Pinecrest", "Quillon",
    "Redfern", "Silverline", "Tidewater", "Umbra", "Vantor", "Willowby", "Xandor", "Yarrow", "Zephyrine",
    "Ashgrove", "Bellmont", "Corvane", "Dunmore", "Elstree", "Fairhaven", "Glenrock", "Harrowgate",
    "Islay", "Jarrah", "Kilnworth", "Lumora", "Marlow", "Nettlefield", "Orrin", "Penhallow", "Quarrystone",
    "Ravelin", "Stonebridge", "Thornbury", "Upland", "Verdance", "Wexley", "Yellowmere", "Zanthe",
    "Quanzent", "Novaris", "Helixion", "Strata", "Lumenor", "Orbique", "Tessaro", "Kaldera", "Mirovia",
)
_NAME_B = {
    "Information Technology": ("Systems", "Software", "Semiconductor", "Data", "Networks", "Logic"),
    "Health Care": ("Therapeutics", "Biosciences", "Medical", "Health", "Genomics", "Pharma"),
    "Financials": ("Capital", "Bancorp", "Financial", "Assurance", "Trust", "Holdings"),
    "Consumer Discretionary": ("Retail", "Motors", "Brands", "Leisure", "Apparel", "Home"),
    "Communication Services": ("Media", "Telecom", "Interactive", "Broadcasting", "Studios"),
    "Industrials": ("Industries", "Aerospace", "Logistics", "Engineering", "Machinery", "Rail"),
    "Consumer Staples": ("Foods", "Beverages", "Household", "Farms", "Provisions"),
    "Energy": ("Energy", "Petroleum", "Resources", "Drilling", "Midstream"),
    "Utilities": ("Power", "Utilities", "Water", "Grid"),
    "Real Estate": ("Realty", "Properties", "REIT", "Estates"),
    "Materials": ("Materials", "Chemicals", "Mining", "Metals", "Packaging"),
}
_SUFFIX = {"US": ("Inc", "Corp", "Co"), "GB": ("plc",), "DE": ("AG", "SE"), "FR": ("SA",), "NL": ("NV",),
           "JP": ("KK", "Holdings KK")}

_CONSONANTS = "BCDFGHJKLMNPQRSTVWXZ"


@dataclass(frozen=True)
class Instrument:
    id: str
    ticker: str
    mic: str
    exch_code: str
    composite: str  # e.g. "QZNT US"
    name: str
    fisn: str
    country: str
    ccy: str
    sector: str
    cfi: str
    is_etf: bool
    isin: str
    sedol: str
    cusip: str | None
    figi: str
    base_px: float
    shs_out: int
    free_float_pct: float
    adv_20d: int
    adv_90d: int
    si_pct: float
    base_util_pct: float
    base_fee_bps: float
    tier: str
    threshold_flag: bool
    index: str | None
    next_ex_div: str | None
    restricted: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def days_to_cover(self) -> float:
        return round(self.si_pct / 100 * self.shs_out / max(self.adv_20d, 1), 2)

    @property
    def etb(self) -> bool:
        return self.tier == "GC" and not self.threshold_flag and not self.restricted

    def to_json(self) -> dict:
        d = asdict(self)
        d["tags"] = list(self.tags)
        d["days_to_cover"] = self.days_to_cover
        d["etb"] = self.etb
        return d


def _ticker(r, style: str, taken: set[str]) -> str:
    while True:
        if style == "numeric":
            t = str(r.randint(1300, 9999))
        else:
            n = r.choice((3, 4, 4, 4))
            t = "".join(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(n))
        if t not in taken:
            taken.add(t)
            return t


def _lognormal(r, median: float, sigma: float) -> float:
    return median * math.exp(r.gauss(0, sigma))


def _draw_tier_metrics(r) -> tuple[float, float, float]:
    """(util %, fee bps, short interest %) with a 70/20/10 GC/Warm/Hot mix."""
    u = r.random()
    if u < 0.70:
        return r.uniform(2, 28), r.uniform(15, 45), r.uniform(0.5, 4)
    if u < 0.90:
        util = r.uniform(30, 82)
        fee = min(max(_lognormal(r, 120, 0.5), 50), 290)
        return util, fee, r.uniform(4, 15)
    util = r.uniform(86, 99)
    fee = min(max(_lognormal(r, 900, 0.7), 310), 6000)
    return util, fee, r.uniform(15, 45)


@lru_cache(maxsize=4)
def _build(seed: int) -> tuple[Instrument, ...]:
    r = rng.fresh("refdata", "universe", seed=seed)
    out: list[Instrument] = []
    used_isin: set[str] = set()
    n = 0
    for m in MARKETS:
        taken: set[str] = set()
        for _ in range(m.count):
            n += 1
            sector = r.choice(SECTORS)
            is_etf = r.random() < 0.04
            ticker = _ticker(r, m.ticker_style, taken)
            if is_etf:
                name = f"{r.choice(_NAME_A)} {r.choice(('Equity', 'Dividend', 'Momentum', 'Value'))} ETF"
                sector = "Fund"
            else:
                name = f"{r.choice(_NAME_A)} {r.choice(_NAME_B[sector])} {r.choice(_SUFFIX[m.country])}"
            # Fictitious identifier ranges (see identifiers.py).
            sedol = ids.make_sedol("9" + "".join(r.choice(ids.SEDOL_CHARS) for _ in range(5)))
            cusip = None
            if m.country == "US":
                cusip = ids.make_cusip(f"99{r.randint(0, 9999):04d}" + f"{r.randint(10, 99)}")
                isin = ids.make_isin("US", cusip)
            elif m.country == "GB":
                isin = ids.make_isin("GB", "00" + sedol)
            else:
                isin = ids.make_isin(m.country, "9Z" + "".join(r.choice(ids.ALNUM) for _ in range(7)))
            if isin in used_isin:  # astronomically unlikely, but keep keys unique
                isin = ids.make_isin(m.country, "9Y" + f"{n:07d}")
            used_isin.add(isin)
            figi = ids.make_figi("".join(r.choice(ids.FIGI_CHARS) for _ in range(8)))

            util, fee, si = _draw_tier_metrics(r)
            threshold = util > 90 and r.random() < 0.35
            shs_out = int(_lognormal(r, 300e6 if m.country != "JP" else 150e6, 0.9))
            shs_out = max(shs_out // 1000 * 1000, 5_000_000)
            adv20 = int(shs_out * _lognormal(r, 0.008, 0.5))
            adv90 = int(adv20 * r.uniform(0.75, 1.25))
            px = round(_lognormal(r, m.px_median, 0.8), 2 if m.ccy != "JPY" else 0)
            ex_div = None
            if not is_etf and r.random() < 0.15:
                ex_div = (date(2026, 10, 28) + timedelta(days=r.randint(0, 30))).isoformat()
            tier = classify(fee, util)
            out.append(
                Instrument(
                    id=f"I{n:05d}",
                    ticker=ticker,
                    mic=r.choice(m.mics),
                    exch_code=m.exch_code,
                    composite=f"{ticker} {m.exch_code}",
                    name=name,
                    fisn=f"{name.split()[0].upper()[:15]}/{'ETF' if is_etf else 'SH'}",
                    country=m.country,
                    ccy=m.ccy,
                    sector=sector,
                    cfi="CEOGEU" if is_etf else "ESVUFR",
                    is_etf=is_etf,
                    isin=isin,
                    sedol=sedol,
                    cusip=cusip,
                    figi=figi,
                    base_px=px,
                    shs_out=shs_out,
                    free_float_pct=round(r.uniform(55, 99), 1),
                    adv_20d=adv20,
                    adv_90d=adv90,
                    si_pct=round(si, 2),
                    base_util_pct=round(util, 2),
                    base_fee_bps=round(fee, 1),
                    tier=tier,
                    threshold_flag=threshold,
                    index=m.index if r.random() < 0.35 else None,
                    next_ex_div=ex_div,
                    restricted=r.random() < 0.004,
                )
            )
    return tuple(out)


def universe(seed: int | None = None) -> tuple[Instrument, ...]:
    return _build(rng.get_seed() if seed is None else seed)


@lru_cache(maxsize=4)
def _index(seed: int) -> dict[str, Instrument]:
    idx: dict[str, Instrument] = {}
    for i in _build(seed):
        idx[i.id] = i
        for key in (i.isin, i.sedol, i.cusip, i.figi, i.composite, f"{i.ticker}.{i.mic}", f"{i.composite} EQUITY"):
            if key:
                idx[key.upper()] = i
    return idx


def instrument_by_id(inst_id: str, seed: int | None = None) -> Instrument | None:
    return _index(rng.get_seed() if seed is None else seed).get(inst_id)


def resolve(query: str, seed: int | None = None) -> Instrument | None:
    """Resolve an ISIN, SEDOL, CUSIP, FIGI, "TICK XX", "TICK XX Equity" or "TICK.MIC"."""
    q = " ".join(query.strip().upper().split())
    return _index(rng.get_seed() if seed is None else seed).get(q)


def by_tier(tier: str, seed: int | None = None) -> list[Instrument]:
    return [i for i in universe(seed) if i.tier == tier and not i.restricted]
