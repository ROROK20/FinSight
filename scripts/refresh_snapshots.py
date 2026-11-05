#!/usr/bin/env python3
"""
Re-cut the bundled SEC snapshots from live companyfacts, under the maintainer's identity.

    SEC_EDGAR_USER_AGENT="Your Name you@yourdomain" python scripts/refresh_snapshots.py

For each file in finsight/data/companyfacts/ it fetches that CIK's companyfacts, keeps
the 10-K facts for the concepts KPIExtractor.XBRL_CONCEPTS reads, for periods ending
after the date the file already states, and writes the JSON and SHA256SUMS. It changes
no value: what SEC answers is what the file holds. Then it prints what moved.

Nothing is written until all five have been fetched and cut, so a failed fetch leaves
the set as it was. --directory writes the cut somewhere else to review first.

The rules are the ones the bundled files state and tests/test_demo_data.py holds them
to. tests/test_refresh_snapshots.py runs this on stubbed payloads, offline; the bundled
files are this script's own output, from a live run on the date they state. What must be
re-derived afterwards is printed at the end, and is in the README.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finsight.extraction.kpi_extractor import KPIExtractor  # noqa: E402
from finsight.ingestion.edgar import EdgarClient, SECUserAgentError, resolve_user_agent  # noqa: E402
from finsight.models.dcf import DCFInputError, DCFModel  # noqa: E402

DIRECTORY = ROOT / "finsight/data/companyfacts"
_WINDOW = re.compile(r"periods ending after (\d{4}-\d{2}-\d{2})")
SOURCE_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

NEXT_STEPS = """\
Now re-derive what stands on these files:
  1. finsight/data/__init__.py: SNAPSHOT_RETRIEVED, and empty KNOWN_SNAPSHOT_GAPS.
  2. python scripts/record_demo.py
  3. The documented values, wherever a test names them: README.md, tests/test_recorded_demo.py,
     tests/test_dcf.py, tests/test_demo_data.py, tests/hand_derivation.py, e2e/smoke.mjs.
  4. TSLA's current debt, above. KNOWN_CURRENT_DEBT in finsight/api/demo_data.py cites its latest
     10-K: update those figures. If total debt now holds current debt, delete that, README's Tesla
     note and its tests in tests/test_current_debt.py.
  5. Re-shoot the two images: node e2e/capture.mjs (its header has the builds it needs)."""


def concepts_read() -> list[str]:
    """The us-gaap concepts the extractor reads."""
    return sorted({c for chain in KPIExtractor.XBRL_CONCEPTS.values() for c, _ in chain})


def window_of(snapshot: dict[str, Any]) -> str:
    """The date a bundled file's facts start after, as its ``contents`` states it."""
    return _WINDOW.search(snapshot["_snapshot"]["contents"]).group(1)


def cut(payload: dict[str, Any], after: str, retrieved: str) -> dict[str, Any]:
    """One SEC payload as a bundled snapshot: 10-K facts for the concepts read, for
    periods ending after ``after``. A value is never changed, only kept or left out."""
    concepts = concepts_read()
    gaap = (payload.get("facts") or {}).get("us-gaap") or {}
    kept: dict[str, Any] = {}
    for concept in concepts:
        node = gaap.get(concept) or {}
        units = {}
        for unit, rows in (node.get("units") or {}).items():
            rows = [r for r in rows if r.get("form") == "10-K" and r.get("end", "") > after]
            if rows:
                units[unit] = rows
        if units:
            kept[concept] = {"label": node.get("label"), "units": units}
    return {
        "_snapshot": {
            "contents": (
                f"SEC EDGAR companyfacts, trimmed: 10-K facts for the {len(concepts)} us-gaap "
                f"concepts KPIExtractor.XBRL_CONCEPTS listed when this file was cut ({retrieved}; "
                f"see concepts), periods ending after {after}. Values are unmodified SEC data. A "
                "concept in concepts that is absent from facts has no 10-K fact for a period in "
                "this window; the filer may have reported it for earlier periods, which this "
                "file does not hold. A concept FinSight reads that is not in concepts (see "
                "not_carried) was never fetched, so whether the filer reports it is unknown "
                "until the file is refreshed."
            ),
            "concepts": concepts,
            "not_carried": [],
            "retrieved": retrieved,
            "source_url": SOURCE_URL.format(cik=payload["cik"]),
        },
        "cik": payload["cik"],
        "entityName": payload.get("entityName"),
        "facts": {"us-gaap": kept},
    }


def dumps(snapshot: dict[str, Any]) -> str:
    """A snapshot as the bundled files write it: one key per line, sorted, the two concept
    lists on one line each, so a refresh diffs by fact."""
    meta = snapshot["_snapshot"]
    head = {"contents": meta["contents"], "concepts": "@concepts", "not_carried": "@not_carried",
            "retrieved": meta["retrieved"], "source_url": meta["source_url"]}
    body = json.dumps({k: v for k, v in snapshot.items() if k != "_snapshot"},
                      indent=1, sort_keys=True, ensure_ascii=False)
    text = json.dumps(head, indent=1, ensure_ascii=False)
    for name in ("concepts", "not_carried"):
        text = text.replace(f'"@{name}"', json.dumps(meta[name], ensure_ascii=False))
    nested = "\n".join((" " + line) for line in text.splitlines())
    return '{\n "_snapshot": ' + nested.lstrip() + ",\n" + body[2:] + "\n"


def checksums(directory: Path) -> str:
    return "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                   for p in sorted(directory.glob("*.json")))


def _rows(snapshot: dict[str, Any]) -> dict[tuple, Any]:
    return {(concept, unit, r["end"], r.get("start"), r.get("accn"), r.get("fp")): r["val"]
            for concept, node in snapshot["facts"]["us-gaap"].items()
            for unit, rows in node["units"].items() for r in rows}


def _latest_year(ticker: str, snapshot: dict[str, Any]) -> tuple[str, str]:
    """(fiscal year, "total debt … · DCF …") from the engine, for the moved list."""
    kpis = KPIExtractor().extract_from_companyfacts(snapshot, ticker)
    debt = kpis.total_debt
    debt_text = "not reported" if debt is None else f"${debt:,.0f}M"
    try:
        value = f"${DCFModel().value_from_kpis(kpis).intrinsic_value_per_share:,.2f}"
    except DCFInputError:
        value = "refused"
    note = kpis.provenance["limitations"].get("current_debt")
    return kpis.period, (f"total debt {debt_text} ({kpis.provenance['concepts'].get('total_debt')}), "
                         f"DCF {value}" + (f"; current debt: {note}" if note else ""))


def moved(ticker: str, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What a refresh changed in one file, fact by fact, then in the engine's answer."""
    a, b = _rows(old), _rows(new)
    lines = []
    for concept in sorted({k[0] for k in a.keys() | b.keys()}):
        added = sum(1 for k in b.keys() - a.keys() if k[0] == concept)
        gone = sum(1 for k in a.keys() - b.keys() if k[0] == concept)
        changed = [k for k in a.keys() & b.keys() if k[0] == concept and a[k] != b[k]]
        if added or gone or changed:
            lines.append(f"{ticker} {concept}: {added} added, {gone} gone, {len(changed)} changed")
        lines += [f"{ticker} {concept} at {k[2]}: {a[k]:,} -> {b[k]:,}" for k in sorted(changed, key=str)]
    if not lines:
        lines.append(f"{ticker}: no fact moved")
    before, after = _latest_year(ticker, old), _latest_year(ticker, new)
    lines.append(f"{ticker} {before[0]}: {before[1]}")
    if after != before:
        lines.append(f"{ticker} {after[0]} now: {after[1]}")
    return lines


def refresh(fetch: Callable[[str], dict[str, Any]], source: Path, target: Path,
            retrieved: str) -> list[str]:
    """Fetch and cut every bundled file, then write them all. ``fetch`` takes a
    zero-padded CIK. Returns what moved."""
    cuts = []
    for path in sorted(source.glob("*.json")):
        old = json.loads(path.read_text(encoding="utf-8"))
        payload = fetch(f"{old['cik']:010d}")
        if payload.get("cik") != old["cik"]:
            raise ValueError(f"{path.stem}: asked for CIK {old['cik']}, got {payload.get('cik')!r}")
        new = cut(payload, window_of(old), retrieved)
        if not new["facts"]["us-gaap"]:
            raise ValueError(f"{path.stem}: nothing left after the cut; refusing to write it")
        cuts.append((path.name, path.stem, old, new))
    target.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, ticker, old, new in cuts:
        (target / name).write_text(dumps(new), encoding="utf-8")
        lines += moved(ticker, old, new)
    (target / "SHA256SUMS").write_text(checksums(target), encoding="utf-8")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--directory", type=Path, default=DIRECTORY,
                        help="where to write the cut (default: the bundled files themselves)")
    args = parser.parse_args(argv)
    try:
        resolve_user_agent()
    except SECUserAgentError as e:
        print(f"Refusing to run, and nothing was requested: {e}", file=sys.stderr)
        return 2
    client = EdgarClient()
    for line in refresh(client.get_company_facts, DIRECTORY, args.directory, date.today().isoformat()):
        print(line)
    print(f"\nwrote {args.directory}\n\n{NEXT_STEPS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
