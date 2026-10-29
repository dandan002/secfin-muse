"""The analytics are pure functions of (seed, instrument, sim time) and agree with the market processes."""

from datetime import UTC, date, datetime

from mock_analytics import metrics
from refdata import market
from refdata.universe import placeholder, universe

T = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


def test_snapshot_is_deterministic_and_consistent():
    inst = placeholder("@HOT:0")
    a, b = metrics.snapshot(inst, T), metrics.snapshot(inst, T)
    assert a == b
    si, borrow = a["shortInterest"], a["borrow"]
    assert borrow["indicativeFeeBps"] == market.fee_bps(inst, T) and borrow["tier"] == market.tier_at(inst, T)
    assert 0 <= borrow["onLoanShares"] <= borrow["lendableShares"] <= metrics.float_shares(inst)
    assert borrow["availableShares"] == borrow["lendableShares"] - borrow["onLoanShares"]
    assert si["daysToCover"] == round(si["shares"] / inst.adv_20d, 2)
    assert 0 <= a["squeezeRisk"] <= a["crowdingScore"] <= 100


def test_hot_names_are_more_crowded_than_gc():
    names = universe()
    hot = [metrics.snapshot(i, T)["crowdingScore"] for i in names if i.tier == "HOT"][:50]
    gc = [metrics.snapshot(i, T)["crowdingScore"] for i in names if i.tier == "GC"][:50]
    assert sum(hot) / len(hot) > 2 * sum(gc) / len(gc)


def test_short_interest_drifts_slowly():
    inst = placeholder("@WARM:3")
    d1 = metrics.si_pct(inst, metrics.close_ts(date(2026, 10, 26)))
    d2 = metrics.si_pct(inst, metrics.close_ts(date(2026, 10, 27)))
    assert d1 != d2 and abs(d2 / d1 - 1) < 0.25


def test_cost_to_borrow_bands():
    assert [metrics.ctb_score(f) for f in (10, 25, 49, 150, 299, 450, 999, 5000)] == [1, 2, 2, 4, 5, 6, 7, 10]


def test_exchange_cycle():
    dates = metrics.settlement_dates(date(2026, 10, 27))
    assert date(2026, 10, 15) in dates and date(2026, 10, 30) in dates  # Oct 31 2026 is a Saturday
    assert metrics.publish_date(date(2026, 10, 15)) == date(2026, 10, 26)
    assert metrics.latest_published(date(2026, 10, 27)) == date(2026, 10, 15)
    rep = metrics.exchange_report(placeholder("@HOT:0:US"), date(2026, 10, 15))
    assert rep["source"] == "EXCHANGE_REPORTED" and rep["currentShortPosition"] > 0


def test_data_date_rolls_at_the_cut():
    assert metrics.data_date(datetime(2026, 10, 27, 20, 59, tzinfo=UTC)) == date(2026, 10, 26)
    assert metrics.data_date(datetime(2026, 10, 27, 21, 0, tzinfo=UTC)) == date(2026, 10, 27)
