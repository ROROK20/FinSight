"""
Documentation truth tests: examples in README.md must print what they claim. These tests
execute the example and compare every printed line with the ``# ...`` output lines written
under each ``print`` in the block.
"""

import contextlib
import pytest
import io
import re
from pathlib import Path

README = Path(__file__).parents[1] / "README.md"


def _python_block_after(heading: str) -> str:
    text = README.read_text(encoding="utf-8")
    section = text[text.index(heading):]
    match = re.search(r"```python\n(.*?)```", section, re.S)
    assert match, f"no python block under {heading!r}"
    return match.group(1)


def _documented_output(code: str) -> list[str]:
    """The comment lines that directly follow each print(...) statement."""
    out, lines = [], code.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("print("):
            for follow in lines[i + 1:]:
                if not follow.startswith("# "):
                    break
                out.append(follow[2:].rstrip())
    return out


def test_readme_dcf_example_prints_what_it_documents():
    code = _python_block_after("## DCF Model")
    expected = _documented_output(code)
    assert expected, "the DCF example documents no output"

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "README.md#dcf-model", "exec"), {})
    actual = [line.rstrip() for line in buffer.getvalue().splitlines()]
    assert actual == expected


# ---------------------------------------------------------------------------
# The product description must describe what ships
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parents[1]
# Where the project describes itself to a reader, an installer, an API client
# or a browser tab.
DESCRIPTION_SITES = [
    "README.md", "setup.py", "finsight/__init__.py", "finsight/api/app.py",
    "frontend/app/layout.tsx", "frontend/app/page.tsx",
]


def test_no_api_route_runs_llm_extraction():
    """The descriptions below say the API reads XBRL only; that stays true
    only while no endpoint calls the LLM text extractor."""
    import ast

    tree = ast.parse((ROOT / "finsight/api/app.py").read_text(encoding="utf-8"))
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "extract_from_text" not in calls


def test_descriptions_do_not_claim_an_llm_pipeline():
    """The README must not claim an LLM pipeline: no endpoint runs one."""
    for site in DESCRIPTION_SITES:
        text = (ROOT / site).read_text(encoding="utf-8")
        for claim in ("AI-powered", "using LLMs", "LLM KPI extraction,"):
            assert claim not in text, f"{site} claims {claim!r}"


def test_no_description_says_sentiment_comes_from_filings():
    """No description may say sentiment comes from filings: it is scored on earnings-call text, its only source
    answered HTTP 404, and so no API mode scores a real call."""
    sites = DESCRIPTION_SITES + ["frontend/README.md", "docs/architecture.md", "docs/usage.md"]
    for site in sites:
        text = (ROOT / site).read_text(encoding="utf-8")
        for claim in ("sentiment from SEC filings", "comparables, and earnings sentiment",
                      "builds a DCF and comparable-company analysis, scores",
                      "DCF, comps, sentiment", "headline financials, DCF, and sentiment",
                      "headline financials, DCF and\nsentiment",
                      "transcripts and filings"):
            assert claim not in text, f"{site} still says {claim!r}"

    layout = (ROOT / "frontend/app/layout.tsx").read_text(encoding="utf-8")
    described = re.findall(r'"([^"]*Sentiment is scored on earnings-call text, not filings[^"]*)"', layout)
    assert len(described) == 2, "both the share card and the page description must say it"
    assert all("labelled synthetic placeholder" in d for d in described)

    readme = README.read_text(encoding="utf-8")
    intro = readme[readme.index("**FinSight reads"):readme.index('<p align="center">')]
    assert "synthetic placeholder transcript" in intro and "not financial advice" in intro
    what = " ".join(readme[readme.index("## What It Does"):readme.index("## Features")].split())
    assert "**earnings-call text**" in what and "no API mode scores a real call" in what
    assert "HTTP 404 when checked on 2026-11-01" in what
    assert "no mode of the API or the dashboard scores a real call" in readme
    for site in ("finsight/__init__.py", "finsight/api/app.py"):
        assert "not filings" in (ROOT / site).read_text(encoding="utf-8").replace("\n", " "), site


def test_the_one_line_description_is_the_readmes_first_sentence():
    import json

    readme = README.read_text(encoding="utf-8")
    first = re.search(r"^\*\*(FinSight reads[^*]+)\*\*$", readme, re.M).group(1)
    setup = " ".join(re.search(r"description=\((.*?)\),", (ROOT / "setup.py").read_text(encoding="utf-8"), re.S)
                     .group(1).split())
    assert setup.replace('" "', "").strip('"') == first
    assert json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))["description"] == first


def test_the_readme_first_screen_says_what_a_reader_needs():
    readme = README.read_text(encoding="utf-8")
    top = readme[:readme.index('<p align="center">')]
    for needed in ("`make demo`", "tests/test_demo_data.py", "not financial advice"):
        assert needed in top, needed
    trust = readme[readme.index("## Why trust the numbers"):readme.index("## Table of Contents")]
    assert trust.count("\n- **") == 5
    for proof in ("tests/test_demo_data.py", "tests/hand_derivation.py", "tests/test_live_path.py",
                  "tests/test_api.py", "e2e/smoke.mjs", "tests/test_recorded_demo.py"):
        assert proof in trust, proof


def test_readme_describes_current_behaviour():
    readme = README.read_text(encoding="utf-8")
    for phrase in ("Until 20", "until 20", "Before 20", "before 20", "Since 20", "since 20", "used to ",
                 "no longer", "old code", "was wrong", "the old "):
        assert phrase not in readme, phrase


def test_the_requirement_files_say_what_ci_installs():
    """The requirement files must say that CI installs the setup.py floors, not those pins, and that requirements.txt
    also pulls unpinned torch, transformers and openai. If CI ever installs a requirements file, this test and
    those sentences change together."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert 'pip install -e ".[dev]"' in ci
    assert "-r requirements" not in ci
    readme = README.read_text(encoding="utf-8")
    for name in ("requirements.txt", "requirements-api.txt"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "CI does NOT install this file" in text, name
        for claim in ("what CI", "CI runs the suite on exactly these pins",
                      "the suite is run\n# on that install in CI"):
            assert claim not in text, f"{name} says {claim!r}"
    assert "CI does not\ninstall either requirements file" in readme
    assert "the exact audited versions" not in readme
    # What is unpinned is what the README says is unpinned.
    pulled = re.findall(r"^(\w[\w-]*)>=", (ROOT / "requirements.txt").read_text(encoding="utf-8"), re.M)
    assert sorted(pulled) == ["openai", "torch", "transformers"]
    assert "unpinned\n" in readme and "openai, transformers and torch" in readme
    assert not re.search(r"^\w[\w-]*>=", (ROOT / "requirements-api.txt").read_text(encoding="utf-8"), re.M)


def test_the_requirement_files_state_no_test_count():
    """The README has the one test count: a count in a pin file is one more thing to go stale."""
    for name in ("requirements.txt", "requirements-api.txt"):
        assert not re.search(r"\b\d+ passed\b", (ROOT / name).read_text(encoding="utf-8")), name


def test_readme_states_the_cash_and_share_definitions_the_code_uses():
    """The README's net-debt and share-count definitions are pinned to the
    extractor: netting marketable securities, or changing the share concept,
    must update the documentation in the same change."""
    from finsight.extraction.kpi_extractor import KPIExtractor

    readme = README.read_text(encoding="utf-8")
    assert KPIExtractor.XBRL_CONCEPTS["cash_and_equivalents"] == (
        ("CashAndCashEquivalentsAtCarryingValue", "USD"),)
    assert KPIExtractor.XBRL_CONCEPTS["diluted_shares"] == (
        ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),)
    assert "marketable securities are **not** netted" in readme
    assert "diluted **weighted-average**" in readme


def test_readme_sentiment_example_prints_what_it_documents(monkeypatch):
    """By default the example finds no transcripts, and prints "Trend: insufficient_data" for them."""
    monkeypatch.delenv("FINSIGHT_LIVE_TRANSCRIPTS", raising=False)
    code = _python_block_after("## Sentiment Analysis")
    expected = _documented_output(code)
    assert expected == ["0 transcripts", "Trend: insufficient_data"]

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "README.md#sentiment-analysis", "exec"), {})
    assert [line.rstrip() for line in buffer.getvalue().splitlines()] == expected


def test_readme_quick_start_prints_what_it_documents(monkeypatch):
    """Runs the Quick Start against the bundled snapshot of the SEC data its
    documented output was produced from."""
    from finsight.data import companyfacts_snapshot
    from finsight.ingestion import edgar

    monkeypatch.setattr(edgar.EdgarClient, "get_cik", lambda self, t: "0000320193")
    monkeypatch.setattr(edgar.EdgarClient, "get_company_facts",
                        lambda self, cik: companyfacts_snapshot("AAPL"))
    code = _python_block_after("## Quick Start")
    expected = _documented_output(code)
    assert len(expected) == 4

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "README.md#quick-start", "exec"), {})
    assert [line.rstrip() for line in buffer.getvalue().splitlines()] == expected


def test_readme_screenshot_caption_matches_the_demo_it_shows():
    """The alt text states the demo-mode figures pictured: they must be what the demo engine computes today
    (re-shoot docs/screenshot.png if they move), and it claims no rating, price target or comps."""
    from finsight.api import demo_data
    from finsight.data import SNAPSHOT_RETRIEVED

    readme = README.read_text(encoding="utf-8")
    alt = re.search(r'<img src="docs/screenshot.png" alt="([^"]+)"', readme).group(1)
    dcf = demo_data.demo_dcf("AAPL")
    meta = demo_data.demo_meta("AAPL")
    value, price = dcf["intrinsic_value_per_share"], meta["current_price"]
    for claim in ("offline demo mode (FINSIGHT_DEMO=1)",
                  f"snapshot (retrieved {SNAPSHOT_RETRIEVED}, {dcf['fiscal_year']} 10-K)",
                  f"price ${price:,.2f} ({meta['price_date']} close)",
                  f"DCF model value ${value:,.2f} (valuation date {dcf['valuation_date']})",
                  f"model vs price {(value / price - 1) * 100:.1f}%"):
        assert claim in alt, claim
    for stale in ("HOLD", "price target", "peer comps", "Price Target"):
        assert stale not in alt


def test_docs_describe_the_word_lists_as_what_they_are():
    """The docs say the word list is curated and inspired by Loughran-McDonald, not that it is that lexicon, and the
    sizes they quote are the code's."""
    from finsight.sentiment import analyzer

    sizes = [len(w.words) + len(w.stems)
             for w in (analyzer.POSITIVE, analyzer.NEGATIVE, analyzer.UNCERTAINTY)]
    readme = README.read_text(encoding="utf-8")
    assert (f"{sum(sizes)}\nhand-picked entries ({sizes[0]} positive, {sizes[1]} negative, "
            f"{sizes[2]} uncertainty)") in readme
    assert f"curated finance word list ({sum(sizes)} tone and uncertainty words" in readme
    arch = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    assert f"{sum(sizes)}\n  hand-picked entries inspired by, but not, the Loughran-McDonald" in arch
    for site in DESCRIPTION_SITES + ["docs/architecture.md", "docs/usage.md"]:
        text = (ROOT / site).read_text(encoding="utf-8")
        for claim in ("Loughran-McDonald lexicon", "LM lexicon", "LM Dict",
                      "Loughran-McDonald financial\n  lexicon"):
            assert claim not in text, f"{site} still says {claim!r}"


def test_render_blueprint_matches_the_readme():
    """README says "Deploy (do not auto-deploy)", so render.yaml has autoDeploy: false, and it lists no OPENAI_API_KEY
    for an API that never calls the LLM."""
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "### Deploy (do not auto-deploy)" in README.read_text(encoding="utf-8")
    assert re.search(r"^\s*autoDeploy:\s*false\s*$", blueprint, re.M)
    assert not re.search(r"^\s*-\s*key:\s*OPENAI_API_KEY", blueprint, re.M)


def test_every_curl_url_with_a_query_is_quoted():
    """An unquoted `curl …/filings/AAPL?form_type=10-K&limit=3` would run in the background at the `&` and lose `limit=3`."""
    for site in ("README.md", "docs/usage.md", "docs/architecture.md", "frontend/README.md"):
        text = (ROOT / site).read_text(encoding="utf-8")
        unquoted = re.findall(r"^\s*curl\s+(?:-\S+\s+)*(http\S*\?\S*)", text, re.M)
        assert unquoted == [], f"{site}: {unquoted}"


def test_no_doc_gives_production_a_localhost_default():
    """A production build without NEXT_PUBLIC_API_URL has no engine, so no doc may give localhost as the plain default."""
    for site in ("docs/usage.md", "docs/architecture.md", "frontend/lib/api.ts"):
        text = (ROOT / site).read_text(encoding="utf-8")
        assert not re.search(r"\(default\s+`?http://localhost:8001", text), site
    assert "under `next dev` only" in (ROOT / "docs/usage.md").read_text(encoding="utf-8")


def test_the_docs_state_how_much_text_extraction_reads():
    """extract_from_text reads the first 8,000 characters, which for a whole
    filing is its cover page; the docs did not say so."""
    from finsight.extraction.kpi_extractor import KPIExtractor
    assert KPIExtractor(max_text_chars=8_000).max_text_chars == 8_000
    assert "first 8,000 characters" in (ROOT / "docs/usage.md").read_text(encoding="utf-8")
    assert "first 8,000 characters" in README.read_text(encoding="utf-8")
    k = KPIExtractor()
    k._client = None
    assert k.extract_from_text("x" * 8_000 + " Revenue of $5 billion", "X", "FY2025").revenue is None
    assert k.extract_from_text("Revenue of $5 billion", "X", "FY2025").revenue == 5_000.0


def test_the_social_card_is_not_older_than_the_screenshot():
    """frontend/public/og.png, the image every shared link shows, is captured by e2e/capture.mjs with the same
    verification as docs/screenshot.png, and may never be older than it."""
    import struct
    import subprocess

    og = ROOT / "frontend/public/og.png"
    width, height = struct.unpack(">II", og.read_bytes()[16:24])
    assert (width, height) == (2400, 1260)
    assert "og: { out: join(FRONTEND, \"public/og.png\")" in (ROOT / "e2e/capture.mjs").read_text()

    older = _card_is_older_than_screenshot(ROOT)
    if older is None:
        pytest.skip("no full git history here (not a checkout, or a shallow clone), so "
                    "git log cannot order the two images; CI's test job fetches it all")
    assert not older, "og.png is older than docs/screenshot.png: re-run e2e/capture.mjs og"


def test_the_social_card_is_shot_from_what_a_shared_link_lands_on():
    """A frontend-only deploy replays the recording under a badge that says so, so the card is captured from that
    state, by a build with no API URL."""
    capture = (ROOT / "e2e/capture.mjs").read_text()
    og = capture[capture.index("  og: { out:"):capture.index("const kids = []")]
    assert 'apiUrl: ""' in og and 'other: "Demo data"' in og
    page = (ROOT / "frontend/app/page.tsx").read_text(encoding="utf-8")
    badge = re.search(r'badge: "([^"]+)"', og).group(1)
    assert f'label: "{badge}"' in page[page.index("  recorded: {"):page.index("  empty: {")]
    readme = README.read_text(encoding="utf-8")
    assert "from the recording a frontend-only deploy shows" in readme


def _card_is_older_than_screenshot(root):
    """Whether og.png last changed before docs/screenshot.png, or None if git cannot tell. On a shallow clone every
    file's last change is the one commit it holds, so the comparison would always pass."""
    import subprocess

    def git(*args):
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""

    if git("rev-parse", "--is-shallow-repository") != "false":
        return None
    og_t = git("log", "-1", "--format=%ct", "--", "frontend/public/og.png")
    shot_t = git("log", "-1", "--format=%ct", "--", "docs/screenshot.png")
    return int(og_t) < int(shot_t) if og_t and shot_t else None


def _image_history(tmp_path, *paths):
    """A repository that commits ``paths`` in order, a day apart."""
    import os
    import subprocess

    repo = tmp_path / "repo"
    for path in ("frontend/public/og.png", "docs/screenshot.png"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)

    def git(*args, date="2026-01-01T00:00:00+0000"):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                        *args], check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})

    git("init", "-q")
    for day, path in enumerate(paths, start=1):
        (repo / path).write_text(str(day))
        git("add", path)
        git("commit", "-qm", path, date=f"2026-01-{day:02d}T00:00:00+0000")
    return repo


OG, SHOT = "frontend/public/og.png", "docs/screenshot.png"


def test_the_card_guard_fails_when_the_card_is_older(tmp_path):
    assert _card_is_older_than_screenshot(_image_history(tmp_path, OG, SHOT)) is True


def test_the_card_guard_passes_when_the_card_is_newer(tmp_path):
    assert _card_is_older_than_screenshot(_image_history(tmp_path, SHOT, OG)) is False


def test_the_card_guard_refuses_to_judge_a_shallow_clone(tmp_path):
    """The depth-1 checkout CI gets by default cannot judge a stale card, so the guard says so."""
    import subprocess

    repo = _image_history(tmp_path, OG, SHOT)
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(clone)],
                   check=True, capture_output=True)
    assert _card_is_older_than_screenshot(clone) is None


def test_cis_test_job_fetches_the_history_the_card_guard_reads():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = ci[ci.index("  test:\n"):ci.index("  frontend:\n")]
    assert "fetch-depth: 0" in job


def test_the_readme_claims_only_what_the_code_does():
    """Each of these sentences would be contradicted by the code."""
    readme = README.read_text(encoding="utf-8")
    for stale in ("all functionality as endpoints", "Net Debt, Guidance", "price target breached",
                  "tests-passing"):
        assert stale not in readme, stale
    assert "actions/workflows/ci.yml/badge.svg" in readme


def test_the_deploy_steps_set_the_share_card_base():
    """Without NEXT_PUBLIC_SITE_URL a production build emits no card, and the deploy
    steps did not mention it."""
    readme = README.read_text(encoding="utf-8")
    deploy = readme[readme.index("### Deploy (do not auto-deploy)"):readme.index("## What It Does")]
    assert "NEXT_PUBLIC_API_URL" in deploy and "`NEXT_PUBLIC_SITE_URL`" in deploy
    assert "no share card" in " ".join(deploy.split())
    site = (ROOT / "frontend/lib/site.ts").read_text(encoding="utf-8")
    layout = (ROOT / "frontend/app/layout.tsx").read_text(encoding="utf-8")
    assert "NEXT_PUBLIC_SITE_URL" in site and 'SITE_URL ? ["/og.png"] : undefined' in layout


def test_the_rest_api_line_names_what_is_library_only():
    """The sensitivity table and the JSON report are not served: nothing in the API
    package touches them, and there is no comps route."""
    from finsight.api.app import app

    routes = {r.path for r in app.routes}
    assert "/report/{ticker}" in routes and not any("comps" in r for r in routes)
    for path in (ROOT / "finsight/api").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "sensitivity_table" not in text and "save_json" not in text, path.name
    readme = README.read_text(encoding="utf-8")
    assert ("a standalone comps call, the DCF sensitivity table and the JSON report are "
            "library-only") in readme


def test_xbrl_sets_no_guidance():
    from finsight.data import companyfacts_snapshot
    from finsight.extraction.kpi_extractor import KPIExtractor

    kpis = KPIExtractor().extract_from_companyfacts(companyfacts_snapshot("AAPL"), "AAPL")
    assert kpis.guidance_revenue_low is None and kpis.guidance_revenue_high is None


def test_the_analyzer_does_not_claim_to_read_filings():
    from finsight.sentiment import analyzer

    assert "and SEC filings" not in analyzer.__doc__
    assert "nothing here reads SEC filings" in " ".join(analyzer.__doc__.split())


def test_the_architecture_doc_names_every_concept_the_snapshots_do_not_carry():
    from finsight.data import KNOWN_SNAPSHOT_GAPS

    arch = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    assert [c for c in KNOWN_SNAPSHOT_GAPS if c not in arch] == []
    assert KNOWN_SNAPSHOT_GAPS == {} and "not_carried` is empty" in arch


def test_nothing_still_speaks_of_synthetic_multiples():
    """No passage may speak of synthetic peer multiples: none exist."""
    arch = " ".join((ROOT / "docs/architecture.md").read_text(encoding="utf-8").split())
    market = " ".join((ROOT / "finsight/ingestion/market_data.py").read_text(encoding="utf-8").split())
    assert "synthetic peer multiples are named" not in arch
    assert "fall back to synthetic multiples" not in market


def test_the_llm_field_count_is_the_prompts():
    from finsight.extraction.kpi_extractor import _KPI_EXTRACTION_PROMPT

    fields = [line for line in _KPI_EXTRACTION_PROMPT.splitlines() if line.startswith("- ")]
    assert len(fields) == 22
    source = (ROOT / "finsight/extraction/kpi_extractor.py").read_text(encoding="utf-8")
    assert "asks for the 22 in _KPI_EXTRACTION_PROMPT" in source
    assert "asks the LLM for 22 fields" in README.read_text(encoding="utf-8")


def test_the_split_wording_does_not_contradict_the_dates_beside_it():
    """The demo sets each 10-K beside one close, and says how far apart they are."""
    readme = " ".join(README.read_text(encoding="utf-8").split())
    demo = (ROOT / "finsight/api/demo_data.py").read_text(encoding="utf-8")
    assert "from one period" not in readme and "from one period" not in demo
    assert "The demo does not run this check: it bundles no split history" in readme
    assert "34 days after AAPL's valuation date" in readme


def test_the_docs_say_what_the_refresh_tests_can_show():
    """Stubs built from the bundle cannot show the rules reproduce the bundle."""
    readme = " ".join(README.read_text(encoding="utf-8").split())
    log = " ".join((ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split())
    assert "reproduces the bundled facts exactly" not in readme
    assert "reproduce the bundled facts exactly" not in log
    assert "exercises the cut offline on stubbed payloads" in readme
    assert "with `mode` live" in readme


def test_security_states_a_dated_count_for_both_audits():
    """Counts age: each one carries the day it was measured."""
    text = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())
    assert "re-audited 2026-11-04" in text and "0 critical / 0 high / 6 moderate" in text
    assert "re-verified 2026-11-04" in text and "resolved 69 packages" in text
    assert "Node 23.9" in text


def test_usage_section_2_leaves_the_kpis_section_3_values():
    """extract_from_text fills no shares or debt, so the usage guide's §3, run after a §2
    that overwrote `kpis` with it, raised DCFInputError."""
    from finsight.extraction.kpi_extractor import KPIExtractor
    from finsight.models.dcf import DCFInputError, DCFModel

    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "\nkpis = extractor.extract_from_text(" not in usage
    assert "\ntext_kpis = extractor.extract_from_text(" in usage
    with pytest.raises(DCFInputError):
        DCFModel().value_from_kpis(KPIExtractor().extract_from_text("Revenue was $5 billion.", "X", "Q"))


def test_the_snapshot_history_is_stated_as_it_happened():
    from finsight import data

    said = " ".join(data.__doc__.split())
    for field in ("``contents``", "``concepts``", "``not_carried``"):
        assert field in said
    assert "`_snapshot.not_carried` is empty" in " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())


def test_teslas_unnetted_securities_sit_beside_its_other_caveats():
    from finsight.api import demo_data

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    head = "**Tesla: marketable securities are not netted.**"
    assert readme.index("**Tesla: the demo's debt excludes current debt.**") < readme.index(head)
    line = " ".join(readme[readme.index(head):readme.index("**A stale base year**")].split())
    per_billion = re.search(r"each \$1B not netted is (\$\d+\.\d\d) a share",
                            demo_data.demo_dcf("TSLA")["limitations"]["marketable_securities"])
    assert f"each $1B not netted is {per_billion.group(1)} a share" in line
    assert "not in the data" in line


def test_the_demo_does_not_claim_a_build_needs_no_network():
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "no network required" not in readme
    assert "The first build needs a network" in readme


def test_comps_lists_only_the_multiples_it_computes():
    from finsight.models import comps

    assert "EV/EBIT," not in comps.__doc__


def test_security_md_states_the_one_address_the_validator_would_accept():
    security = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())
    assert "no address in a shipped file would pass" not in security
    assert "with one exception: the project's own contact" in security


def test_the_readme_says_a_10k_a_is_not_read():
    """The extraction reads the form 10-K exactly, so a restatement only on an amendment is not picked up."""
    from finsight.ingestion.edgar import extract_financial_series

    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "so a restatement wins" not in readme
    assert "a 10-K/A is not" in readme
    row = {"start": "2025-01-01", "end": "2025-12-31", "fy": 2025}
    facts = {"facts": {"us-gaap": {"OperatingIncomeLoss": {"units": {"USD": [
        {**row, "val": 2_000, "form": "10-K", "filed": "2026-02-01", "accn": "k"},
        {**row, "val": 500, "form": "10-K/A", "filed": "2026-06-01", "accn": "amended"},
    ]}}}}}
    (entry,) = extract_financial_series(facts, "OperatingIncomeLoss", unit="USD")
    assert (entry["val"], entry["accn"]) == (2_000, "k")


def test_security_says_what_yfinance_writes():
    """SECURITY.md's list of what is written to disk includes yfinance's own cache on the live path."""
    from pathlib import Path

    import yfinance

    security = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())
    assert "Nothing else is persisted" not in security
    cache = (Path(yfinance.__file__).parent / "cache.py").read_text(encoding="utf-8")
    assert '"py-yfinance"' in cache
    for name in ("tkr-tz.db", "cookies.db"):
        assert name in cache and f"`{name}`" in security
    assert "`py-yfinance/`" in security
