"""The project imports and serves its app with only its own dependencies installed."""

from mock_control.app import app, spec


def test_app_builds():
    paths = set(app.openapi()["paths"])
    assert {"/health", "/_admin/clock", "/_admin/reset"} <= paths
    assert spec.name == "mock-control" and spec.schema == "control"


def test_openapi_snapshot_is_current():
    """openapi.json is the published contract; regenerate with: uv run python -m mockcore.openapi mock_control.app"""
    from pathlib import Path

    from mockcore.openapi import render

    assert (Path(__file__).parents[1] / "openapi.json").read_text() == render("mock_control.app")
