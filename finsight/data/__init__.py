"""
Recorded SEC EDGAR data bundled with FinSight.

``companyfacts/<TICKER>.json`` are real SEC EDGAR companyfacts payloads for the
five demo tickers, retrieved on :data:`SNAPSHOT_RETRIEVED` and trimmed three ways:
to 10-K facts, to the us-gaap concepts
:class:`~finsight.extraction.kpi_extractor.KPIExtractor` read ON THAT DAY, and to
periods ending after a per-file date (about five fiscal years). The values
themselves are unmodified SEC data. Each file's ``_snapshot`` key records its
source URL, the window (``contents``), the exact concept list (``concepts``) and
any concept FinSight has read since that it does not carry (``not_carried``; see
:data:`KNOWN_SNAPSHOT_GAPS`).

So a concept in ``concepts`` that is absent from ``facts`` has no 10-K fact for a
period IN THAT WINDOW, and says nothing about earlier years: Apple's real payload
carries ``Revenues`` through FY2018, before its file's window. A ``not_carried``
concept was never fetched, so it is unknown until a refresh.

``companyfacts/SHA256SUMS`` pins the files' bytes (``shasum -a 256 -c`` reads it),
and tests/test_demo_data.py fails if a file stops matching it. The same tests hold
each file to the three rules above. ``scripts/refresh_snapshots.py`` cuts the files
from a live fetch, under the maintainer's identity, and last did so on
:data:`SNAPSHOT_RETRIEVED`.

Demo mode runs the live extraction path over them, and the test suite uses them as
offline, real-shaped fixtures.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SNAPSHOT_RETRIEVED = "2026-11-04"

# Concepts KPIExtractor reads that the bundled snapshots do not carry, and why.
# tests/test_demo_data.py fails if the extractor reads a concept that is neither in a
# snapshot's `concepts` nor listed here. Empty: every concept was fetched at the last cut.
KNOWN_SNAPSHOT_GAPS: dict[str, str] = {}
_DIR = Path(__file__).parent / "companyfacts"


def snapshot_tickers() -> list[str]:
    return sorted(p.stem for p in _DIR.glob("*.json"))


@lru_cache(maxsize=None)
def _load(ticker: str) -> dict[str, Any]:
    with open(_DIR / f"{ticker}.json", encoding="utf-8") as f:
        return json.load(f)


def snapshot_checksum_problems(directory: Path = _DIR) -> list[str]:
    """Where the bundled files differ from ``SHA256SUMS`` (empty when they match)."""
    pinned = dict(reversed(line.split(maxsplit=1))
                  for line in (directory / "SHA256SUMS").read_text().splitlines())
    held = {p.name for p in directory.glob("*.json")}
    problems = [f"{name}: not in SHA256SUMS" for name in sorted(held - pinned.keys())]
    problems += [f"{name}: in SHA256SUMS, not on disk" for name in sorted(pinned.keys() - held)]
    problems += [f"{name}: sha-256 does not match SHA256SUMS" for name in sorted(held & pinned.keys())
                 if hashlib.sha256((directory / name).read_bytes()).hexdigest() != pinned[name]]
    return problems


def snapshot_meta(facts: dict[str, Any]) -> dict[str, Any] | None:
    """The ``_snapshot`` block of a bundled payload (None for live SEC data)."""
    meta = facts.get("_snapshot") if isinstance(facts, dict) else None
    return meta if isinstance(meta, dict) else None


def companyfacts_snapshot(ticker: str) -> dict[str, Any]:
    """The bundled companyfacts snapshot for ``ticker`` (treat as read-only).

    Raises
    ------
    KeyError
        If no snapshot is bundled for the ticker.
    """
    t = ticker.upper()
    if not (_DIR / f"{t}.json").is_file():
        raise KeyError(f"No bundled companyfacts snapshot for {t}")
    return _load(t)
