"""
Truth checks on the dashboard source (frontend/), run with the Python suite.

The dashboard has no unit-test runner of its own, and the claims pinned here
are visible in the source text: a panel that would show fabricated figures for
real, named companies must not exist at all.
"""

import re
import subprocess
import textwrap
from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "frontend"
PAGE = (FRONTEND / "app/page.tsx").read_text(encoding="utf-8")
RESEARCH = (FRONTEND / "lib/research.ts").read_text(encoding="utf-8")
CI = (FRONTEND.parent / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_no_hash_generated_value_path_is_left():
    """A deployed frontend has no backend, so what it shows is what the build
    carries. The mock put Apple at a $220.00 DCF and an EV of $3.1T beside its real
    name, and a hash of any other ticker's name made up the rest. The
    page now shows engine responses, recorded or live, and nothing else."""
    assert not (FRONTEND / "lib/mockResearch.ts").exists()
    sources = [*FRONTEND.glob("lib/*.ts"), *FRONTEND.glob("app/*.tsx")]
    assert len(sources) >= 5
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for stale in ("seededRand", "hash(", "Math.random", "getResearch", "mergeLiveData",
                      "mockResearch", "COMPANIES", "SAMPLE_PEERS", "195.42", "modelValue: 220",
                      "383_285", "Sample peer"):
            assert stale not in text, f"{path.name}: {stale}"


def test_panels_with_no_endpoint_are_gone_and_say_why():
    """A peer table, multi-year charts and catalyst/risk lists have no endpoint: the page names them and shows none,
    and its charts plot the /dcf response's own projection."""
    for stale in ("<SampleTag", "SampleTag =", "data.comps", "revenueHistory", "marginHistory",
                  "data.catalysts", "data.risks", "Key Risks", "Peer Comparison", "Mkt Cap"):
        assert stale not in PAGE, stale
    assert "Not on this dashboard" in PAGE
    assert "shows none rather than invent them" in " ".join(PAGE.split())
    assert "data.projection" in PAGE
    assert "dcf.projected_revenues_m" in RESEARCH and "dcf.projected_fcfs_m" in RESEARCH


def test_the_recording_badge_says_what_it_is():
    """The badge a no-backend visitor sees states all three facts."""
    copy = PAGE[PAGE.index("  recorded: {"):PAGE.index("  empty: {")]
    label = re.search(r'label: "([^"]+)"', copy).group(1)
    for claim in ("recording", "bundled sec snapshots", "no live backend"):
        assert claim in label.lower(), claim
    assert "RECORDING.captured" in copy


def test_a_ticker_the_recording_does_not_hold_shows_a_reason_and_no_panel():
    assert 'const noData = mode === "empty" || mode === "uncovered";' in PAGE
    assert "{!noData && (" in PAGE
    assert PAGE.index("{!noData && (") < PAGE.index("{/* Hero card */}")
    assert "no figure is shown" in PAGE
    assert "setData(recorded ?? emptyResearch(ticker));" in PAGE


def test_a_failed_call_reads_unavailable_and_nothing_replaces_it():
    assert '(missing(part) ? "Unavailable" : "Not reported")' in PAGE
    assert "UnavailableTag" in PAGE


def test_model_vs_price_shows_the_valuation_date_and_the_price_date():
    """The hero card compared a fiscal-year-end model value with a later close
    and dated neither side of the comparison."""
    hero = PAGE[PAGE.index("Model vs price"):PAGE.index("A mechanical DCF output")]
    assert "valuation date {data.valuationDate}" in hero
    assert "close ${data.priceDate}" in hero
    assert re.search(r"out\.valuationDate = vd", RESEARCH)
    assert "dcf.valuation_date" in RESEARCH


def test_every_guidance_class_has_copy_and_is_accepted():
    """The engine's guidance classes changed; the dashboard kept an
    invented tone for any class outside four, so a real "not_stated" would have
    shown "raised" under an engine badge."""
    from finsight.sentiment.analyzer import GUIDANCE_TONES

    ts = re.search(r"export const GUIDANCE_TONES = \[(.*?)\] as const", RESEARCH, re.S).group(1)
    assert re.findall(r'"(\w+)"', ts) == list(GUIDANCE_TONES)
    copy = re.search(r"const GUIDANCE_COPY: Record<GuidanceTone, string> = \{(.*?)\};", PAGE, re.S).group(1)
    assert sorted(re.findall(r"(\w+):", copy)) == sorted(GUIDANCE_TONES)
    assert ': "not_stated";' in RESEARCH  # an unknown class reads not stated
    assert "GUIDANCE_COPY[data.guidanceTone]" in PAGE


def test_the_merge_unit_tests_run_in_ci():
    """frontend/tests/merge.test.mjs pins that an engine null stays null; CI must run it, and the README count is its."""
    import json
    pkg = json.loads((FRONTEND / "package.json").read_text())
    assert pkg["scripts"]["test"] == "node --test tests/*.test.mjs"
    ci = (FRONTEND.parent / ".github/workflows/ci.yml").read_text()
    assert "run: npm test" in ci
    n = sum(len(re.findall(r"^test\(", f.read_text(), re.M))
            for f in (FRONTEND / "tests").glob("*.test.mjs"))
    readme = (FRONTEND.parent / "README.md").read_text()
    assert re.search(rf"no extra dependency; {n} tests, re-run 20\d\d-\d\d-\d\d", readme)


def test_a_refused_dcf_is_not_a_failed_call():
    """A 422 from /dcf keeps its reason, so the panel shows that and no made-up value."""
    load = PAGE[PAGE.index("const load = useCallback"):PAGE.index("const missing = (part: Part)")]
    assert 'dcf: dcfRefusal ? "refused" : partState(dcf),' in load
    assert "mergeEngineData(ticker, { meta, kpis, dcf, sentiment, dcfRefusal })" in load
    assert PAGE.count("{data.dcfRefusal}") == 2  # the hero card and the DCF panel


def test_next_uses_the_patched_postcss_and_security_says_so():
    """Next 15 vendors postcss 8.4.31, a high in npm audit. The override gives it the
    build's own; removing the override reopens the high."""
    import json
    root = FRONTEND.parent
    pkg = json.loads((FRONTEND / "package.json").read_text())
    assert pkg["overrides"]["next"]["postcss"] == "$postcss"
    lock = json.loads((FRONTEND / "package-lock.json").read_text())
    assert "node_modules/next/node_modules/postcss" not in lock["packages"]
    sec = (root / "SECURITY.md").read_text()
    assert "**0 critical / 0 high / 6 moderate**" in sec    # measured 2026-11-04
    assert "No accepted exception remains." in sec
    for stale in ("0 critical / 1 high / 1 moderate", "project owner's\n  decision",
                  "Next 15.x still vendors"):
        assert stale not in sec, stale


def test_no_figure_is_rendered_while_loading():
    """Under "Loading engine…" the page rendered an invented price and model
    value (and shipped them in the prerendered HTML)."""
    assert 'const pending = mode === "loading";' in PAGE
    assert 'pending ? "…" : n == null ?' in PAGE
    assert 'useState<CompanyResearch>(() => emptyResearch("AAPL"))' in PAGE


def test_a_stale_response_is_dropped():
    """load() never cancelled the previous request, so a slow reply for the
    previous ticker overwrote the current one."""
    load = PAGE[PAGE.index("const load = useCallback"):PAGE.index("const missing = (part: Part)")]
    assert "const seq = ++loadSeq.current;" in load
    assert load.count("if (stale()) return;") == 3


def _prerender_step() -> str:
    """The body of CI's prerender check, exactly as CI runs it."""
    block = CI[CI.index("- name: Prerendered HTML"):]
    return textwrap.dedent(block[block.index("run: |\n") + len("run: |\n"):])


def _run_prerender_step(tmp_path, html: str, chunk: str = "") -> int:
    app = tmp_path / ".next/server/app"
    static = tmp_path / ".next/static/chunks"
    app.mkdir(parents=True)
    static.mkdir(parents=True)
    (app / "index.html").write_text(html)
    (static / "page.js").write_text(chunk)
    # `bash -e` is what GitHub Actions runs a default-shell step with.
    return subprocess.run(["bash", "-e", "-c", _prerender_step()], cwd=tmp_path,
                          capture_output=True).returncode


def test_ci_fails_when_the_prerender_carries_localhost(tmp_path):
    """A production build without NEXT_PUBLIC_API_URL / NEXT_PUBLIC_SITE_URL must not call each visitor's localhost or
    put it in its share cards. `bash -e` ignores `!`, so a `! grep -q` on a non-final line cannot fail: this runs the
    step itself."""
    assert _run_prerender_step(tmp_path, "<html>http://localhost:3000</html>") != 0


def test_ci_fails_when_the_build_carries_an_invented_figure(tmp_path):
    assert _run_prerender_step(tmp_path, "<html>ok</html>", 'x={modelValue:220,y:195.42}') != 0


def test_ci_passes_a_clean_build(tmp_path):
    assert _run_prerender_step(tmp_path, "<html>ok</html>", "x={value:106.7053}") == 0


def test_the_resolvers_keep_a_production_build_off_localhost():
    api = (FRONTEND / "lib/api.ts").read_text()
    assert 'return env.NODE_ENV === "development" ? "http://localhost:8001" : null;' in api
    assert "if (!API_URL || path === null) return null;" in api


def test_the_two_free_cash_flows_carry_two_names():
    """Tesla's reported FCF is $6.2B and its modelled Year 1 $0.7M: one name cannot hold both."""
    assert 'label: "Free Cash Flow"' not in PAGE
    assert 'label: "Reported FCF"' in PAGE
    assert "DCF projection: free cash flow" not in PAGE
    assert "DCF projection: modelled FCF" in PAGE
    assert "stock comp stays a cost" in PAGE
