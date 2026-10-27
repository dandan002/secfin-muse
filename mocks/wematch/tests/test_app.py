"""The project imports and serves its app with only its own dependencies installed."""

from mock_wematch.app import app, spec


def test_app_builds():
    paths = set(app.openapi()["paths"])
    assert {"/health", "/_admin/clock", "/_admin/reset"} <= paths
    assert spec.name == "mock-wematch" and spec.schema == "wematch"
