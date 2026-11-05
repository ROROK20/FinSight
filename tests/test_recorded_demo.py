"""
The dashboard's no-backend state replays frontend/lib/recordedDemo.json, so a
deploy with no API shows what the engine computes, not what a mock made up. This
regenerates the recording the way scripts/record_demo.py does and fails on any
drift: change the engine, or a snapshot, and the recording must be retaken.
"""

import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
RECORDING = ROOT / "frontend/lib/recordedDemo.json"

_spec = importlib.util.spec_from_file_location("record_demo", ROOT / "scripts/record_demo.py")
record_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record_demo)


@pytest.fixture(scope="module")
def committed():
    return json.loads(RECORDING.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fresh():
    return record_demo.record()


def test_the_recording_is_what_the_engine_answers_today(committed, fresh):
    assert record_demo.drift(committed, fresh) == []


def test_the_drift_check_catches_a_tampered_figure(committed, fresh):
    tampered = copy.deepcopy(committed)
    tampered["tickers"]["AAPL"]["dcf"]["intrinsic_value_per_share"] = 220.0
    assert record_demo.drift(tampered, fresh) == ["/dcf/AAPL"]


def test_the_drift_check_ignores_last_bit_float_noise_only(committed, fresh):
    """A clean room on Python 3.11 failed on /dcf/TSLA: its numpy differs from
    3.12's in a value's last bits. That is not drift; a cent's change still is."""
    noisy = copy.deepcopy(committed)
    value = noisy["tickers"]["TSLA"]["dcf"]["intrinsic_value_per_share"]
    noisy["tickers"]["TSLA"]["dcf"]["intrinsic_value_per_share"] = value * (1 + 1e-14)
    assert record_demo.drift(noisy, fresh) == []
    noisy["tickers"]["TSLA"]["dcf"]["intrinsic_value_per_share"] = value + 0.01
    assert record_demo.drift(noisy, fresh) == ["/dcf/TSLA"]


def test_the_drift_check_catches_a_ticker_added_or_dropped(committed, fresh):
    tampered = copy.deepcopy(committed)
    tampered["tickers"]["AMZN"] = tampered["tickers"].pop("TSLA")
    assert record_demo.drift(tampered, fresh) == [
        "AMZN: in only one of the two", "TSLA: in only one of the two"]


def test_the_recording_covers_the_demo_tickers_and_the_dashboards_endpoints(committed):
    from finsight.api import demo_data

    assert list(committed["tickers"]) == list(demo_data.DEMO_TICKERS)
    api = (ROOT / "frontend/lib/api.ts").read_text(encoding="utf-8")
    called = set(re.findall(r'seg\("/(\w+)/"', api))
    assert called == set(record_demo.ENDPOINTS)
    for ticker, answers in committed["tickers"].items():
        assert set(answers) == called, ticker
        for endpoint, body in answers.items():
            assert body["mode"] == "demo", (ticker, endpoint)


def test_the_recording_is_dated(committed):
    from finsight.data import SNAPSHOT_RETRIEVED

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", committed["captured"])
    assert committed["captured"] >= SNAPSHOT_RETRIEVED == committed["snapshot_retrieved"]
    assert committed["generator"] == "scripts/record_demo.py"


def test_the_recorded_values_are_the_documented_ones(committed):
    values = {t: round(a["dcf"]["intrinsic_value_per_share"], 2)
              for t, a in committed["tickers"].items()}
    assert values == {"AAPL": 165.31, "MSFT": 313.23, "NVDA": 127.44, "GOOGL": 142.65, "TSLA": 13.19}


def test_the_recorded_sentiment_is_labelled_synthetic(committed):
    for ticker, answers in committed["tickers"].items():
        assert answers["sentiment"]["synthetic"] is True, ticker
        assert all(r["synthetic"] for r in answers["sentiment"]["results"]), ticker


def test_the_recording_holds_no_peer_data(committed):
    """So the dashboard has no peer table offline, and says why."""
    assert "comps" not in json.dumps(committed["tickers"]).lower()
