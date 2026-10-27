"""Seeded, independent random streams.

``stream("ngt", "counter-spread")`` always returns the same ``random.Random`` for that key,
seeded from sha256(global seed, key). Adding a new consumer never shifts another
consumer's sequence. ``hash_*`` helpers are stateless: the same inputs always give the
same number, which is what the market-data processes use.
"""

from __future__ import annotations

import hashlib
import math
import random

_seed: int = 20261027
_streams: dict[tuple[str, ...], random.Random] = {}


def set_seed(seed: int) -> None:
    global _seed
    _seed = seed
    _streams.clear()


def get_seed() -> int:
    return _seed


def _digest(*parts: object, seed: int | None = None) -> bytes:
    key = "|".join(str(p) for p in (_seed if seed is None else seed, *parts))
    return hashlib.sha256(key.encode()).digest()


def stream(*parts: str) -> random.Random:
    key = tuple(parts)
    rnd = _streams.get(key)
    if rnd is None:
        rnd = random.Random(int.from_bytes(_digest("stream", *parts)[:8], "big"))
        _streams[key] = rnd
    return rnd


def fresh(*parts: object, seed: int | None = None) -> random.Random:
    """A new generator for a key (not cached): use for one-shot deterministic generation."""
    return random.Random(int.from_bytes(_digest("fresh", *parts, seed=seed)[:8], "big"))


def hash_uniform(*parts: object) -> float:
    return (int.from_bytes(_digest("u", *parts)[:8], "big") + 0.5) / 2**64


def hash_normal(*parts: object) -> float:
    d = _digest("n", *parts)
    u1 = (int.from_bytes(d[:8], "big") + 0.5) / 2**64
    u2 = (int.from_bytes(d[8:16], "big") + 0.5) / 2**64
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
