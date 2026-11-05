"""
Tests for the SEC EDGAR client: requester identity, fair access, and the XBRL
series reader.

Identity. The SEC asks every automated client to identify ITSELF, so a clone
that configured nothing must not send live traffic under someone else's name.
These tests pin the contract: no real address lives in the client source, the
User-Agent comes only from ``SEC_EDGAR_USER_AGENT``, and a live request without
one fails loudly with instructions instead of borrowing an identity.

Fair access. One process-wide limiter spaces request starts across threads and
client instances; the ticker map and companyfacts payloads are not re-fetched
needlessly.

Series. Share counts live under ``units.shares`` (not ``units.USD``) and a
10-K's quarterly sub-period facts must not replace the fiscal-year figure.

Runs fully offline — every request goes to a stub session.
"""

import re
import threading
from pathlib import Path

import pytest
import requests

from finsight.ingestion import edgar

PROJECT_CONTACT = "rohan.s.khiani.vora@gmail.com"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# RFC 2606 reserved documentation domains — never anyone's real mailbox.
_RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
# A structurally valid identity for the tests below. It never leaves this
# process: every request in this file goes to a stub session.
VALID_UA = "FinSight Test Suite finsight-tests@university.edu"


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def _stub_session(client, payload_for_url, calls):
    """Replace the client's HTTP session with a recorder that never hits the network."""

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Resp(payload_for_url(url))

    client._session.get = fake_get


_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_client_source_carries_no_real_address():
    """No fallback identity can exist if no real address is in the source."""
    found = set(_EMAIL_RE.findall(Path(edgar.__file__).read_text(encoding="utf-8")))
    real = {a for a in found if not a.lower().endswith(_RESERVED_DOMAINS)}
    assert not real, f"edgar.py hard-codes real address(es): {sorted(real)}"


def test_package_metadata_uses_only_the_project_contact():
    setup_py = Path(edgar.__file__).parents[2] / "setup.py"
    found = set(_EMAIL_RE.findall(setup_py.read_text(encoding="utf-8")))
    assert found <= {PROJECT_CONTACT}, sorted(found - {PROJECT_CONTACT})


def test_live_request_without_user_agent_fails_with_instructions(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    client = edgar.EdgarClient(rate_limiter=edgar.RateLimiter(0))
    calls = []
    _stub_session(client, lambda url: _TICKERS, calls)

    with pytest.raises(edgar.SECUserAgentError) as exc:
        client.get_cik("AAPL")

    message = str(exc.value)
    assert "SEC_EDGAR_USER_AGENT" in message
    assert "YOUR OWN name" in message
    assert "FINSIGHT_DEMO=1" in message  # tells the user the offline route
    assert calls == [], "a request went out without the caller's identity"


@pytest.mark.parametrize("value", ["   ", "Your Name your-email@example.com", "FinSight research bot"])
def test_unusable_user_agents_are_rejected(monkeypatch, value):
    """Blank, the shipped placeholder, and a value with no contact email all
    identify nobody, so none of them may be sent."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", value)
    with pytest.raises(edgar.SECUserAgentError):
        edgar.resolve_user_agent()


# The example in the docs, and every other shape of address that reaches nobody.
@pytest.mark.parametrize("value", [
    "FinSight demo demo@example.com",
    "Jane Doe jane.doe@example.org",
    "Bot bot@mail.example.net",
    "Ops ops@company.invalid",
    "Dev dev@finsight.test",
    "Shop info@shop.example",
    "Box admin@box.localhost",
    "Acme Research noreply@acme-research.com",
    "Dev 1234+dev@users.noreply.github.com",
])
def test_undeliverable_contacts_are_rejected(monkeypatch, value):
    """An invented contact is worse than none: the SEC sees an identity, and
    it reaches nobody."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", value)
    with pytest.raises(edgar.SECUserAgentError, match="no deliverable contact email"):
        edgar.resolve_user_agent()


def test_a_reserved_address_beside_a_real_one_is_accepted(monkeypatch):
    value = "Jane Doe jane@example.com jane.doe@university.edu"
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", value)
    assert edgar.resolve_user_agent() == value


def test_an_undeliverable_identity_never_reaches_the_session(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "FinSight demo demo@example.com")
    client = edgar.EdgarClient(rate_limiter=edgar.RateLimiter(0))
    calls = []
    _stub_session(client, lambda url: _TICKERS, calls)
    with pytest.raises(edgar.SECUserAgentError):
        client.get_cik("AAPL")
    assert calls == [], "a request went out under an undeliverable identity"


_ROOT = Path(edgar.__file__).parents[2]
_SKIP_DIRS = {".git", "node_modules", ".next", "tests", "__pycache__", "data",
              ".pytest_cache", "finsight.egg-info"}
_TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml", ".txt", ".ts", ".tsx",
                  ".mjs", ".js", ".json", ".example", ".cfg"}
# Where the project's published contact legitimately appears (as a contact,
# never as a requester identity).
_CONTACT_FILES = {"README.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "setup.py",
                  "bug_report.yml", "config.yml"}


# Directories that are never part of a checkout's shipped files when git is
# unavailable and the tree has to be walked instead (virtualenvs, build output).
_UNSHIPPED_DIRS = {".git", "node_modules", ".next", "build", "dist", "__pycache__",
                   ".pytest_cache", ".venv", "venv", "env", ".tox", ".mypy_cache"}


def _tracked_paths(root: Path) -> list[Path] | None:
    """Every git-tracked file under ``root``, or None when git cannot say."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
                             check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [root / p for p in out.decode().split("\0") if p]


def _walked_paths(root: Path) -> list[Path]:
    paths = []
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts[:-1]
        if any(p in _UNSHIPPED_DIRS or p.startswith(("venv", ".venv")) for p in parts):
            continue
        if any((root.joinpath(*parts[:i + 1]) / "pyvenv.cfg").exists() for i in range(len(parts))):
            continue
        paths.append(path)
    return paths


def _shipped_text_files(root: Path = None):
    """The files a checkout ships: ``git ls-files`` (a virtualenv or build output
    inside the checkout is not shipped, so a contributor's ``.venv/`` cannot fail the
    identity check on third-party packages)."""
    root = root or _ROOT
    paths = _tracked_paths(root)
    if paths is None:
        paths = _walked_paths(root)
    for path in paths:
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]) or not path.is_file():
            continue
        if path.name == "package-lock.json":
            continue
        if path.suffix in _TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile"}:
            yield rel, path


def _identity_offenders(root: Path = None) -> list[str]:
    offenders = []
    for rel, path in _shipped_text_files(root):
        for local, domain in re.findall(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
                                        path.read_text(encoding="utf-8", errors="ignore")):
            address = f"{local}@{domain}"
            if edgar.undeliverable_contact(local, domain):
                continue
            if address == PROJECT_CONTACT and path.name in _CONTACT_FILES:
                continue
            offenders.append(f"{rel}: {address}")
    return offenders


def test_no_shipped_file_carries_an_identity_the_validator_would_accept():
    """README and SECURITY.md say no fallback identity ships. Every address in
    a shipped file is either undeliverable (so it can never be sent, even if
    copied verbatim) or the project contact in a contact document."""
    offenders = _identity_offenders()
    assert not offenders, offenders


def _checkout_with_a_venv(tmp_path: Path, tracked_address: str | None) -> Path:
    import subprocess
    (tmp_path / "finsight").mkdir()
    (tmp_path / "finsight" / "client.py").write_text(
        f"UA = 'FinSight {tracked_address}'\n" if tracked_address else "UA = None\n")
    venv = tmp_path / "venv3.12" / "lib" / "site-packages" / "packaging"
    venv.mkdir(parents=True)
    (tmp_path / "venv3.12" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "__init__.py").write_text("__email__ = 'maintainer@gmail.com'\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "finsight/client.py"], check=True)
    return tmp_path


def test_a_virtualenv_inside_the_checkout_is_not_scanned(tmp_path):
    root = _checkout_with_a_venv(tmp_path, tracked_address=None)
    assert _identity_offenders(root) == []


def test_a_tracked_offender_is_still_caught(tmp_path):
    root = _checkout_with_a_venv(tmp_path, tracked_address="someone@gmail.com")
    assert _identity_offenders(root) == ["finsight/client.py: someone@gmail.com"]


def test_without_git_the_walk_skips_virtualenvs(tmp_path, monkeypatch):
    root = _checkout_with_a_venv(tmp_path, tracked_address="someone@gmail.com")
    import tests.test_edgar_client as me
    monkeypatch.setattr(me, "_tracked_paths", lambda r: None)
    assert me._identity_offenders(root) == ["finsight/client.py: someone@gmail.com"]


def test_compose_passes_through_only_the_users_identity():
    """docker-compose.yml fell back to "FinSight demo demo@example.com"."""
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    (value,) = re.findall(r"^\s*SEC_EDGAR_USER_AGENT:\s*(.*?)\s*$", compose, re.M)
    assert value in ("${SEC_EDGAR_USER_AGENT:-}", "${SEC_EDGAR_USER_AGENT}", ""), value


def test_configured_user_agent_is_sent_even_when_set_after_construction(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    client = edgar.EdgarClient(rate_limiter=edgar.RateLimiter(0))
    calls = []
    _stub_session(client, lambda url: _TICKERS, calls)

    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", VALID_UA)
    assert client.get_cik("aapl") == "0000320193"
    assert calls[0]["headers"]["User-Agent"] == VALID_UA


def test_constructing_a_client_needs_no_configuration(monkeypatch):
    """Demo mode holds a client at import time; that must not require a UA."""
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    edgar.EdgarClient()


# ---------------------------------------------------------------------------
# Fair access
# ---------------------------------------------------------------------------

def test_requests_are_spaced_process_wide_across_threads_and_clients(monkeypatch):
    """Six concurrent requests from TWO default clients must start at least one
    limiter interval apart. A per-call sleep with no shared lock lets all six
    sleep in parallel and fire together, multiplying the rate by the thread
    count (FastAPI runs sync endpoints in a 40-thread pool)."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", VALID_UA)

    # A virtual clock per thread: sleep() advances only the sleeping thread's
    # time, so the recorded start times are exactly what the limiter scheduled,
    # independent of how loaded the machine running the suite is.
    local = threading.local()

    class _VirtualTime:
        @staticmethod
        def monotonic():
            return getattr(local, "now", 1_000.0)

        @staticmethod
        def sleep(seconds):
            local.now = getattr(local, "now", 1_000.0) + seconds

    monkeypatch.setattr(edgar, "time", _VirtualTime)

    starts = []
    lock = threading.Lock()

    def fake_get(url, **kwargs):
        with lock:
            starts.append(_VirtualTime.monotonic())
        return _Resp({})

    clients = [edgar.EdgarClient(), edgar.EdgarClient()]
    for c in clients:
        c._session.get = fake_get

    threads = [
        threading.Thread(target=clients[i % 2]._get, args=("https://data.sec.gov/x",))
        for i in range(6)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) == 6
    # 0.1 s is the SEC's 10 requests/second ceiling expressed as a spacing.
    assert min(gaps) >= 0.1, f"requests not spaced: gaps {gaps}"


def test_shared_limiter_stays_under_the_sec_ceiling():
    assert edgar.SEC_RATE_LIMITER.min_interval >= 0.1  # <= 10 requests/second


def test_unknown_tickers_do_not_redownload_the_ticker_map(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", VALID_UA)
    client = edgar.EdgarClient(rate_limiter=edgar.RateLimiter(0))
    calls = []
    _stub_session(client, lambda url: _TICKERS, calls)

    for bogus in ("ZZZZ", "QQQQ", "ZZZZ"):
        with pytest.raises(ValueError, match="CIK not found"):
            client.get_cik(bogus)
    assert client.get_cik("MSFT") == "0000789019"
    assert len(calls) == 1, f"ticker map downloaded {len(calls)} times"


def test_company_facts_are_cached_per_cik(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", VALID_UA)
    client = edgar.EdgarClient(rate_limiter=edgar.RateLimiter(0))
    calls = []
    _stub_session(client, lambda url: {"cik": 320193, "facts": {}}, calls)

    a = client.get_company_facts("0000320193")
    b = client.get_company_facts("0000320193")
    assert a is b
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# XBRL series
# ---------------------------------------------------------------------------

def _facts(concept, unit, entries, taxonomy="us-gaap"):
    return {"facts": {taxonomy: {concept: {"units": {unit: entries}}}}}


def test_share_counts_are_read_from_the_shares_unit():
    """SEC files share counts under units.shares: reading them under USD would find nothing."""
    facts = _facts("CommonStockSharesOutstanding", "shares", [
        {"end": "2025-09-27", "val": 14_773_260_000, "form": "10-K",
         "filed": "2025-10-31", "fy": 2025, "accn": "0000320193-25-000079"},
    ])
    series = edgar.extract_financial_series(facts, "CommonStockSharesOutstanding")
    assert [e["val"] for e in series] == [14_773_260_000]
    assert series[0]["unit"] == "shares"
    # The client method behaves identically.
    assert edgar.EdgarClient().extract_financial_series(
        facts, "CommonStockSharesOutstanding") == series


def test_quarterly_facts_in_a_10k_do_not_replace_the_fiscal_year():
    q4 = {"start": "2025-06-29", "end": "2025-09-27", "val": 102_466_000_000,
          "form": "10-K", "filed": "2025-10-31", "fy": 2025}
    fy = {"start": "2024-09-29", "end": "2025-09-27", "val": 416_161_000_000,
          "form": "10-K", "filed": "2025-10-31", "fy": 2025}
    facts = _facts("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", [q4, fy])
    series = edgar.extract_financial_series(
        facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
    assert [e["val"] for e in series] == [416_161_000_000]


def test_restatement_wins_but_the_period_keeps_its_own_fiscal_year_label():
    original = {"start": "2022-09-25", "end": "2023-09-30", "val": 383_285_000_000,
                "form": "10-K", "filed": "2023-11-03", "fy": 2023, "accn": "a-2023"}
    re_reported = {**original, "filed": "2025-10-31", "fy": 2025, "accn": "a-2025"}
    facts = _facts("RevenueFromContractWithCustomerExcludingAssessedTax", "USD",
                   [re_reported, original])
    (entry,) = edgar.extract_financial_series(
        facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
    assert entry["accn"] == "a-2025"      # latest filing's value
    assert entry["first_fy"] == 2023      # but it is FY2023, not FY2025
    assert entry["first_filed"] == "2023-11-03"


def test_filing_text_is_streamed_with_a_byte_cap(monkeypatch):
    """get_filing_text truncates as it streams, so a huge body is never held in memory whole."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", VALID_UA)
    client = edgar.EdgarClient(rate_limiter=edgar.RateLimiter(0))
    pulled = []

    class _Streamed:
        encoding = "utf-8"
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            for _ in range(10_000):              # a 655 MB "filing"
                pulled.append(chunk_size)
                yield b"<p>" + b"x" * (chunk_size - 3)

        def close(self):
            pulled.append("closed")

        @property
        def text(self):
            raise AssertionError("the whole body was read")

    calls = []
    def _get(url, **kw):
        calls.append(kw)
        return _Streamed()
    client._session.get = _get
    filing = edgar.Filing(cik="1", accession_number="a", form_type="10-K", filing_date="",
                          period_of_report="", company_name="X", url="https://www.sec.gov/x.htm")
    text = client.get_filing_text(filing, max_chars=1_000)
    assert calls[0].get("stream") is True
    assert len([p for p in pulled if p != "closed"]) == 1 and pulled[-1] == "closed"
    assert 0 < len(text) <= 1_000


def test_the_sec_urls_are_the_ones_the_snapshots_came_from(monkeypatch):
    """The bundled source_url is .../companyfacts/CIK0000320193.json; the client asked
    for .../companyfacts/0000320193.json, and /submissions/ the same way."""
    from finsight.ingestion import edgar

    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "FinSight Test Suite finsight-tests@university.edu")
    asked = []

    class _Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"filings": {"recent": {}}, "name": "x", "facts": {}}

    client = edgar.EdgarClient()
    monkeypatch.setattr(client._session, "get", lambda url, **k: asked.append(url) or _Response())
    client.get_company_facts("0000320193")
    client.get_filings("0000320193")
    assert asked == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
                     "https://data.sec.gov/submissions/CIK0000320193.json"]
    from finsight.data import companyfacts_snapshot

    assert companyfacts_snapshot("AAPL")["_snapshot"]["source_url"] == asked[0]
