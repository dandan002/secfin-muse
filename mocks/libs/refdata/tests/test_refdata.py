from datetime import UTC, datetime, timedelta

import pytest

from mockcore import rng
from refdata import identifiers as ids
from refdata import market
from refdata.counterparties import counterparties
from refdata.universe import resolve, snapshot, universe

needs_snapshot = pytest.mark.skipif(snapshot() is None, reason="no public snapshot (run `refdata fetch`)")


def test_known_real_world_check_digits():
    # Public examples of each scheme (used only to validate the algorithms).
    assert ids.is_valid_isin("US0378331005")
    assert ids.is_valid_isin("GB0002634946")
    assert ids.is_valid_cusip("037833100")
    assert ids.is_valid_sedol("0263494")
    assert ids.is_valid_sedol("B0YBKJ7")
    assert ids.is_valid_lei("5493001KJTIIGC8Y1R12")
    assert ids.is_valid_figi("BBG000B9XRY4") and ids.is_valid_figi("BBG000BLNNH6")  # Apple, IBM
    assert not ids.is_valid_figi("BBG000B9XRY5")
    assert not ids.is_valid_isin("US0378331006")
    assert not ids.is_valid_cusip("037833101")
    assert not ids.is_valid_sedol("0263495")


def test_make_round_trips():
    assert ids.make_isin("US", "037833100") == "US0378331005"
    assert ids.make_cusip("03783310") == "037833100"
    assert ids.make_sedol("026349") == "0263494"
    lei = ids.make_lei("MOCK00ABCDEFGHIJKL")
    assert ids.is_valid_lei(lei)


def test_detect_and_suggest():
    assert ids.detect("US0378331005") == "ISIN"
    assert ids.detect("037833100") == "CUSIP"
    assert ids.detect("0263494") == "SEDOL"
    assert "0263494" in ids.suggest_fix("0263495")
    assert "US0378331005" in ids.suggest_fix("US0378331006")


def test_synthetic_universe_is_deterministic_and_valid():
    a = universe(1, "synthetic")
    b = universe(1, "synthetic")
    assert a == b
    assert len(a) == 2000
    assert universe(2, "synthetic")[0] != a[0]
    for inst in a:
        assert ids.is_valid_isin(inst.isin), inst
        assert ids.is_valid_sedol(inst.sedol) and inst.sedol.startswith("9")
        assert ids.is_valid_figi(inst.figi)
        if inst.cusip:
            assert ids.is_valid_cusip(inst.cusip) and inst.cusip.startswith("99")
            assert inst.isin[2:11] == inst.cusip
    tiers = {t: sum(1 for i in a if i.tier == t) for t in ("GC", "WARM", "HOT")}
    assert 1250 < tiers["GC"] < 1550
    assert 300 < tiers["WARM"] < 500
    assert 120 < tiers["HOT"] < 280
    assert len({i.isin for i in a}) == 2000
    assert all(i.source == "synthetic" and i.figi.startswith("MKG") for i in a)


@needs_snapshot
def test_public_universe_is_real_listings_with_fictitious_licensed_ids():
    a = universe(1, "public")
    assert a == universe(1, "public") and len(snapshot()["instruments"]) >= len(a) > 1500
    assert len({i.isin for i in a}) == len(a) == len({i.figi for i in a})
    for inst in a:
        assert inst.source == "public" and inst.figi.startswith("BBG") and ids.is_valid_figi(inst.figi)
        assert ids.is_valid_isin(inst.isin) and inst.sedol.startswith("9")  # licensed ids stay fictitious
        assert inst.base_px > 0 and inst.shs_out > 0 and 0.005 <= inst.px_vol_daily <= 0.12
    by_country = {c: [i for i in a if i.country == c] for c in ("US", "GB", "DE", "FR", "NL", "JP")}
    for country, insts in by_country.items():  # every market has every tier, close to 70/20/10
        share = {t: sum(i.tier == t for i in insts) / len(insts) for t in ("GC", "WARM", "HOT")}
        assert 0.6 < share["GC"] < 0.8 and 0.12 < share["WARM"] < 0.28 and 0.06 < share["HOT"] < 0.16, country
    us = by_country["US"]
    gc_cap = sorted(i.mkt_cap or 0 for i in us if i.tier == "GC")
    hot_cap = sorted(i.mkt_cap or 0 for i in us if i.tier == "HOT")
    assert hot_cap[len(hot_cap) // 2] < gc_cap[len(gc_cap) // 2] / 5  # specials skew small
    assert resolve("AAPL US", 1).name.startswith("Apple")
    assert resolve("BRK.B US", 1) is resolve("BRK/B US", 1) is not None

def test_resolve_by_every_identifier():
    rng.set_seed(7)
    inst = universe()[123]  # whichever source is active
    for q in (inst.isin, inst.sedol, inst.figi, inst.composite, inst.composite.lower() + " equity",
              f"{inst.ticker}.{inst.mic}"):
        assert resolve(q) is inst, q


def test_counterparties():
    cps = counterparties(1)
    assert len(cps) == 25
    assert len({c.code for c in cps}) == 25
    assert len({c.ngt_code for c in cps}) == 25
    for c in cps:
        assert ids.is_valid_lei(c.lei) and c.lei.startswith("MOCK")
    assert {c.persona for c in cps if c.persona} == {
        "pb-alpha", "pb-bravo", "harbor-lending", "kestrel-hf", "delta-one", "juniper-hf"
    }


def test_market_processes_are_pure_functions_of_time():
    rng.set_seed(11)
    inst = universe()[42]
    t = datetime(2026, 10, 27, 13, 31, 12, tzinfo=UTC)
    f1 = market.fee_bps(inst, t)
    market._log_fee_path.cache_clear()
    assert market.fee_bps(inst, t) == f1
    # Continuity: one second later is close.
    assert abs(market.fee_bps(inst, t + timedelta(seconds=1)) - f1) / f1 < 0.02
    assert market.price(inst, t) > 0
    assert 0 < market.util_pct(inst, t) < 100
