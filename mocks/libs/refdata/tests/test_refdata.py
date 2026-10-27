from datetime import UTC, datetime, timedelta

from mockcore import rng
from refdata import identifiers as ids
from refdata import market
from refdata.counterparties import counterparties
from refdata.universe import resolve, universe


def test_known_real_world_check_digits():
    # Public examples of each scheme (used only to validate the algorithms).
    assert ids.is_valid_isin("US0378331005")
    assert ids.is_valid_isin("GB0002634946")
    assert ids.is_valid_cusip("037833100")
    assert ids.is_valid_sedol("0263494")
    assert ids.is_valid_sedol("B0YBKJ7")
    assert ids.is_valid_lei("5493001KJTIIGC8Y1R12")
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


def test_universe_is_deterministic_and_valid():
    a = universe(1)
    b = universe(1)
    assert a == b
    assert len(a) == 2000
    assert universe(2)[0] != a[0]
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


def test_resolve_by_every_identifier():
    rng.set_seed(7)
    inst = universe()[123]
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
