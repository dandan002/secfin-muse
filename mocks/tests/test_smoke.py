"""The full demo, played end to end by scripts/smoke.py's stand-in platform."""

from smoke import Smoke


def test_demo_end_to_end(mocks):
    s = Smoke(mocks.urls, mocks.stack.fix_port)
    assert s.run(), [r for r in s.results if not r[1]]
