#!/usr/bin/env python3
"""
Record what the FinSight engine answers in demo mode, for the dashboard's
no-backend state.

A frontend deployed with no API behind it shows whatever the build carries. It
carries this recording: the engine's own responses, run
in-process with ``FINSIGHT_DEMO=1`` over the bundled SEC snapshots, for the same
endpoints the dashboard calls. tests/test_recorded_demo.py regenerates it and
fails on any drift, so it cannot outlive the engine it records.

    python scripts/record_demo.py           # rewrite frontend/lib/recordedDemo.json
    python scripts/record_demo.py --check   # exit 1 if it has drifted; writes nothing

No network: demo mode makes no SEC, Yahoo or transcript request. The capture date
is the only thing that changes between two runs on the same engine.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "lib" / "recordedDemo.json"
ENDPOINTS = ("meta", "kpis", "dcf", "sentiment")


def record() -> dict[str, Any]:
    """The engine's answers for every demo ticker, as the dashboard would get them."""
    from fastapi.testclient import TestClient

    from finsight.api.app import app
    from finsight.data import SNAPSHOT_RETRIEVED

    previous = os.environ.get("FINSIGHT_DEMO")
    os.environ["FINSIGHT_DEMO"] = "1"
    try:
        client = TestClient(app)
        tickers = {}
        for ticker in client.get("/health").json()["demo_tickers"]:
            answers = {}
            for endpoint in ENDPOINTS:
                response = client.get(f"/{endpoint}/{ticker}")
                if response.status_code != 200:
                    raise RuntimeError(f"/{endpoint}/{ticker}: HTTP {response.status_code}")
                answers[endpoint] = response.json()
            tickers[ticker] = answers
    finally:
        if previous is None:
            del os.environ["FINSIGHT_DEMO"]
        else:
            os.environ["FINSIGHT_DEMO"] = previous
    return {"snapshot_retrieved": SNAPSHOT_RETRIEVED, "tickers": tickers}


def _same(a: Any, b: Any) -> bool:
    """Equal, floats to 1 part in 1e9: the supported Python and numpy versions
    differ in a value's last bits, and no figure is shown to that precision."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return False
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    return a == b


def drift(committed: dict[str, Any], fresh: dict[str, Any]) -> list[str]:
    """Where a committed recording differs from a fresh one (the capture date aside)."""
    problems = []
    if committed.get("snapshot_retrieved") != fresh["snapshot_retrieved"]:
        problems.append("snapshot_retrieved")
    held, new = committed.get("tickers", {}), fresh["tickers"]
    for ticker in sorted(held.keys() | new.keys()):
        if ticker not in held or ticker not in new:
            problems.append(f"{ticker}: in only one of the two")
            continue
        for endpoint in ENDPOINTS:
            if not _same(held[ticker].get(endpoint), new[ticker][endpoint]):
                problems.append(f"/{endpoint}/{ticker}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="compare only; write nothing")
    args = parser.parse_args()

    fresh = record()
    if args.check:
        problems = drift(json.loads(OUT.read_text(encoding="utf-8")), fresh)
        for problem in problems:
            print(f"drifted: {problem}", file=sys.stderr)
        return 1 if problems else 0

    payload = {"captured": date.today().isoformat(),
               "generator": "scripts/record_demo.py", **fresh}
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False, allow_nan=False) + "\n",
                   encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
