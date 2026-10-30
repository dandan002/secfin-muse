"""The project imports and serves its app with only its own dependencies installed."""

from mock_analytics.app import app, spec


def test_app_builds():
    paths = set(app.openapi()["paths"])
    assert {"/health", "/_admin/clock", "/_admin/reset", "/v1/securities/{ref}", "/v1/mapping"} <= paths
    assert spec.name == "mock-analytics" and spec.schema == "analytics"


def test_openapi_snapshot_is_current():
    """openapi.json is the published contract; regenerate with: uv run python -m mockcore.openapi mock_analytics.app"""
    from pathlib import Path

    from mockcore.openapi import render

    assert (Path(__file__).parents[1] / "openapi.json").read_text() == render("mock_analytics.app")
