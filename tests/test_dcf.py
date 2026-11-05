"""Tests for DCF valuation model."""

import pytest
from finsight.models.dcf import DCFModel, DCFAssumptions, DCFResult


@pytest.fixture
def simple_assumptions():
    return DCFAssumptions(
        base_revenue=10_000.0,  # $10B
        revenue_growth_rates=[0.10, 0.08, 0.07, 0.06, 0.05],
        terminal_growth_rate=0.03,
        ebitda_margin=0.25,
        da_as_pct_revenue=0.04,
        capex_as_pct_revenue=0.05,
        nwc_pct_of_revenue_change=0.01,
        tax_rate=0.21,
        wacc=0.10,
        net_debt=2_000.0,
        shares_outstanding=1_000.0,
    )


class TestDCFModel:
    def test_run_returns_result(self, simple_assumptions):
        model = DCFModel()
        result = model.run(simple_assumptions)
        assert isinstance(result, DCFResult)

    def test_intrinsic_value_positive(self, simple_assumptions):
        model = DCFModel()
        result = model.run(simple_assumptions)
        assert result.intrinsic_value_per_share > 0

    def test_enterprise_value_positive(self, simple_assumptions):
        model = DCFModel()
        result = model.run(simple_assumptions)
        assert result.enterprise_value > 0

    def test_projected_revenues_length(self, simple_assumptions):
        model = DCFModel(n_years=5)
        result = model.run(simple_assumptions)
        assert len(result.projected_revenues) == 5

    def test_projected_revenues_increasing(self, simple_assumptions):
        """With positive growth, revenues should increase each year."""
        model = DCFModel()
        result = model.run(simple_assumptions)
        for i in range(len(result.projected_revenues) - 1):
            assert result.projected_revenues[i+1] > result.projected_revenues[i]

    def test_higher_wacc_lower_valuation(self, simple_assumptions):
        model = DCFModel()
        r_low_wacc = model.run(simple_assumptions)

        high_wacc_assumptions = DCFAssumptions(**{**simple_assumptions.__dict__, "wacc": 0.15})
        r_high_wacc = model.run(high_wacc_assumptions)

        assert r_high_wacc.intrinsic_value_per_share < r_low_wacc.intrinsic_value_per_share

    def test_equity_value_net_debt_relationship(self, simple_assumptions):
        model = DCFModel()
        result = model.run(simple_assumptions)
        assert abs(result.equity_value - (result.enterprise_value - simple_assumptions.net_debt)) < 0.01

    def test_sensitivity_table_shape(self, simple_assumptions):
        model = DCFModel()
        result = model.run(simple_assumptions)
        assert not result.sensitivity_table.empty
        assert result.sensitivity_table.shape[0] > 0

    def test_value_from_kpis(self):
        from finsight.extraction.kpi_extractor import ExtractedKPIs
        kpis = ExtractedKPIs(
            ticker="TEST",
            period="FY2023",
            source_type="10-K",
            revenue=5_000.0,
            revenue_growth_yoy=0.12,
            ebitda=1_000.0,
            total_debt=2_000.0,
            cash_and_equivalents=500.0,
            capex=250.0,
            shares_outstanding=500.0,
        )
        model = DCFModel()
        result = model.value_from_kpis(kpis)
        assert result.intrinsic_value_per_share > 0

    def test_terminal_value_pct_reasonable(self, simple_assumptions):
        """Terminal value typically 60-80% of total EV."""
        model = DCFModel()
        result = model.run(simple_assumptions)
        assert 0.40 < result.terminal_value_pct < 0.95

    def test_net_debt_reduces_equity_value(self):
        """Higher net debt = lower equity value."""
        model = DCFModel()
        base = DCFAssumptions(base_revenue=1000, net_debt=0, shares_outstanding=100)
        high_debt = DCFAssumptions(base_revenue=1000, net_debt=5000, shares_outstanding=100)
        r_base = model.run(base)
        r_debt = model.run(high_debt)
        assert r_debt.intrinsic_value_per_share < r_base.intrinsic_value_per_share


# ---------------------------------------------------------------------------
# Regression pins
# ---------------------------------------------------------------------------

def _flat(**overrides):
    base = dict(
        base_revenue=1_000.0, revenue_growth_rates=[0.0] * 5, terminal_growth_rate=0.03,
        ebitda_margin=0.30, da_as_pct_revenue=0.04, capex_as_pct_revenue=0.04,
        tax_rate=0.21, wacc=0.10, net_debt=0.0, shares_outstanding=100.0,
    )
    base.update(overrides)
    return DCFAssumptions(**base)


class TestWorkingCapital:
    def test_no_revenue_change_means_no_working_capital_investment(self):
        """ΔNWC is charged on the revenue CHANGE, not its level, so flat revenue puts no cash into working capital."""
        result = DCFModel().run(_flat(), compute_sensitivity=False)
        expected = 1_000.0 * ((0.30 - 0.04) * (1 - 0.21) + 0.04 - 0.04)  # 205.4
        assert result.projected_fcfs == pytest.approx([expected] * 5)

    def test_working_capital_is_charged_on_the_revenue_change(self):
        a = _flat(revenue_growth_rates=[0.10] * 5, nwc_pct_of_revenue_change=0.10)
        result = DCFModel().run(a, compute_sensitivity=False)
        rev1 = 1_100.0
        expected_y1 = rev1 * ((0.30 - 0.04) * 0.79) - 0.10 * (rev1 - 1_000.0)
        assert result.projected_fcfs[0] == pytest.approx(expected_y1)

    def test_terminal_year_carries_terminal_rate_working_capital(self):
        """The perpetuity is a normalised year n+1 growing at g, so its ΔNWC
        reflects g — not the faster growth of the final explicit year."""
        a = _flat(revenue_growth_rates=[0.10] * 5, nwc_pct_of_revenue_change=0.10)
        r = DCFModel().run(a, compute_sensitivity=False)
        rev_n = r.projected_revenues[-1]
        rev_t = rev_n * 1.03
        fcf_t = rev_t * ((0.30 - 0.04) * 0.79) - 0.10 * (rev_t - rev_n)
        assert r.terminal_value == pytest.approx(fcf_t / (0.10 - 0.03))


class TestSpreadGuard:
    @pytest.mark.parametrize("wacc,g", [(0.05, 0.05), (0.05, 0.049), (0.04, 0.05)])
    def test_wacc_must_exceed_terminal_growth_by_the_minimum_spread(self, wacc, g):
        """wacc == g divided by zero; a 0.1 pp spread priced the terminal value
        at 1,000x FCF ($5,896.74/share for the AAPL demo)."""
        with pytest.raises(ValueError, match="must exceed the terminal growth"):
            DCFModel().run(_flat(wacc=wacc, terminal_growth_rate=g))

    def test_sensitivity_cells_inside_the_spread_are_blank(self):
        r = DCFModel().run(_flat(wacc=0.07, terminal_growth_rate=0.03))
        table = r.sensitivity_table
        assert table.loc["WACC=5.0%", "TGR=4.0%"] != table.loc["WACC=5.0%", "TGR=4.0%"]  # NaN
        assert table.loc["WACC=7.0%", "TGR=5.0%"] > 0  # exactly the 2 pp minimum


def _kpis(**kw):
    from finsight.extraction.kpi_extractor import ExtractedKPIs
    base = dict(ticker="TEST", period="FY2025", source_type="10-K",
                revenue=10_000.0, shares_outstanding=500.0, ebitda=2_500.0,
                operating_income=2_000.0, capex=400.0)
    base.update(kw)
    return ExtractedKPIs(**base)


class TestInferredAssumptions:
    def test_a_shrinking_company_is_modelled_as_shrinking(self):
        """A company whose revenue fell 10% is projected to shrink: year-1 growth is not floored at the terminal rate."""
        a = DCFModel()._infer_assumptions(_kpis(revenue_growth_yoy=-0.10))
        assert a.revenue_growth_rates[0] == pytest.approx(-0.10)
        assert a.revenue_growth_rates[-1] == pytest.approx(0.03)
        r = DCFModel().run(a, compute_sensitivity=False)
        assert r.projected_revenues[0] == pytest.approx(9_000.0)

    def test_a_collapse_is_clamped_and_the_clamp_is_disclosed(self):
        a = DCFModel()._infer_assumptions(_kpis(revenue_growth_yoy=-0.60))
        assert a.revenue_growth_rates[0] == pytest.approx(DCFModel.MIN_YEAR1_GROWTH)
        assert "clamped" in a.sources["revenue_growth_rates"]

    @pytest.mark.parametrize("debt,cash,expected", [
        (2_000.0, None, 2_000.0), (None, 500.0, -500.0), (2_000.0, 500.0, 1_500.0),
    ])
    def test_net_debt_uses_whichever_side_is_reported(self, debt, cash, expected):
        """Net debt counts the side it has: it does not need BOTH debt and cash."""
        a = DCFModel()._infer_assumptions(
            _kpis(total_debt=debt, cash_and_equivalents=cash))
        assert a.net_debt == pytest.approx(expected)

    def test_net_debt_with_neither_side_is_a_disclosed_default(self):
        a = DCFModel()._infer_assumptions(_kpis())
        assert a.net_debt == 0.0
        assert a.sources["net_debt"].startswith("default:")

    @pytest.mark.parametrize("missing", ["revenue", "shares_outstanding"])
    def test_missing_required_fundamentals_refuse_rather_than_placeholder(self, missing):
        """Missing revenue or shares is a refusal, never a placeholder of 1,000."""
        with pytest.raises(ValueError, match=missing) as exc:
            DCFModel().value_from_kpis(_kpis(**{missing: None}))
        from finsight.models.dcf import DCFInputError
        assert isinstance(exc.value, DCFInputError)

    def test_every_inferred_input_states_its_source(self):
        a = DCFModel()._infer_assumptions(_kpis(revenue_growth_yoy=0.05), wacc=0.09)
        assert a.sources["wacc"] == "caller"
        assert a.sources["ebitda_margin"].startswith("reported EBITDA")
        assert a.sources["da_as_pct_revenue"].startswith("reported")
        assert a.da_as_pct_revenue == pytest.approx(0.05)    # (2,500 − 2,000) / 10,000
        assert a.capex_as_pct_revenue == pytest.approx(0.04)
        assert a.sources["nwc_pct_of_revenue_change"].startswith("default:")
        # A missing margin is a refusal (no placeholder fundamental).
        from finsight.models.dcf import DCFInputError
        with pytest.raises(DCFInputError, match="neither operating income nor EBITDA"):
            DCFModel()._infer_assumptions(_kpis(ebitda=None, operating_income=None))


class TestCapexFade:
    """CapEx starts at the reported intensity and moves in equal steps to the D&A rate by year n, and the terminal
    year stays at D&A. Swapping in the D&A rate from year 1 would take Microsoft's FY2026 34.9% of revenue to 10.3%,
    tripling its modelled year-1 FCF."""

    # _kpis: revenue 10,000, EBITDA 2,500, operating income 2,000 → D&A 5%.
    def _a(self, **kw):
        return DCFModel()._infer_assumptions(_kpis(revenue_growth_yoy=0.0, **kw))

    def test_year1_capex_is_the_reported_intensity(self):
        r = DCFModel().run(self._a(capex=3_490.0), compute_sensitivity=False)
        # Flat year-1 revenue, so no working capital: FCF = NOPAT + D&A − CapEx.
        assert r.projected_fcfs[0] == pytest.approx(10_000.0 * ((0.25 - 0.05) * 0.79 + 0.05 - 0.349))
        assert r.projected_capex_pct[0] == pytest.approx(0.349)

    def test_capex_moves_in_equal_steps_to_the_da_rate_by_year_n(self):
        path = DCFModel().run(self._a(capex=3_490.0), compute_sensitivity=False).projected_capex_pct
        steps = [b - a for a, b in zip(path, path[1:])]
        assert steps == pytest.approx([(0.05 - 0.349) / 4] * 4)
        assert path[-1] == pytest.approx(0.05)

    @pytest.mark.parametrize("capex", [3_490.0, 200.0])
    def test_the_terminal_year_invests_at_the_da_rate(self, capex):
        """Steady state is CapEx = D&A from either side: above-D&A investment
        fades down to it, below-D&A investment (a shrinking asset base) up."""
        r = DCFModel().run(self._a(capex=capex), compute_sensitivity=False)
        rev_n = r.projected_revenues[-1]
        rev_t = rev_n * 1.03
        fcf_t = rev_t * (0.25 - 0.05) * 0.79 - 0.02 * (rev_t - rev_n)  # D&A − CapEx = 0
        assert r.terminal_value == pytest.approx(fcf_t / (0.10 - 0.03))

    def test_the_fade_is_disclosed_as_an_adjustment(self):
        a = self._a(capex=3_490.0)
        note = a.adjustments["capex_as_pct_revenue"]
        assert "reported 34.9%" in note and "5.0% D&A rate by year 5" in note
        assert "capped" not in a.sources["capex_as_pct_revenue"]

    def test_unreported_capex_is_a_disclosed_default_at_the_da_rate(self):
        a = self._a(capex=None)
        assert a.capex_as_pct_revenue == pytest.approx(0.05)
        assert a.sources["capex_as_pct_revenue"].startswith("default:")
        assert "capex_as_pct_revenue" not in a.adjustments

    def test_hand_built_assumptions_keep_capex_flat(self):
        r = DCFModel().run(_flat(capex_as_pct_revenue=0.30), compute_sensitivity=False)
        assert r.projected_capex_pct == pytest.approx([0.30] * 5)


class TestAdjustments:
    """Every departure from a reported figure is listed, not only the fade."""

    def test_a_clamp_a_margin_bound_and_an_unreported_side_are_all_listed(self):
        a = DCFModel()._infer_assumptions(_kpis(
            revenue_growth_yoy=-0.60, ebitda=7_000.0, operating_income=6_500.0,
            total_debt=None, cash_and_equivalents=500.0))
        assert "clamped to -30.0%" in a.adjustments["revenue_growth_rates"]
        assert a.adjustments["ebitda_margin"].startswith("reported 70.0% clamped to 60.0%")
        assert a.adjustments["net_debt"] == "debt not reported for FY2025, counted as 0"

    def test_defaults_are_not_double_counted_as_adjustments(self):
        # Margin inputs stay reported (a missing margin refuses).
        a = DCFModel()._infer_assumptions(_kpis(capex=None))
        assert a.sources["revenue_growth_rates"].startswith("default:")
        assert set(a.adjustments).isdisjoint(
            k for k, v in a.sources.items() if v.startswith("default"))


class TestDefinitionsAreStated:
    """The sources say what the figures are: net debt nets cash and cash equivalents only, and the share count is
    the diluted weighted average, or the period-end BASIC count when that is what the extractor fell back to."""

    def _kpis(self, shares_concept):
        k = _kpis(total_debt=2_000.0, cash_and_equivalents=500.0)
        k.provenance = {"concepts": {
            "total_debt": "LongTermDebt + CommercialPaper (derived)",
            "cash_and_equivalents": "CashAndCashEquivalentsAtCarryingValue",
            "shares_outstanding": shares_concept,
        }}
        return k

    def test_the_cash_definition_is_named_and_securities_are_said_to_be_excluded(self):
        a = DCFModel()._infer_assumptions(self._kpis("WeightedAverageNumberOfDilutedSharesOutstanding"))
        note = a.sources["net_debt"]
        assert "(LongTermDebt + CommercialPaper (derived))" in note
        assert "cash and cash equivalents (CashAndCashEquivalentsAtCarryingValue)" in note
        # Widened 2026-11-02: non-current securities, noncontrolling interests
        # and preferred stock are ignored too, and the source now says so.
        assert note.endswith("marketable securities (current and non-current) are not "
                             "netted, and noncontrolling interests and preferred stock are "
                             "not deducted from equity value")

    def test_the_share_count_says_it_is_a_weighted_average(self):
        a = DCFModel()._infer_assumptions(self._kpis("WeightedAverageNumberOfDilutedSharesOutstanding"))
        assert a.sources["shares_outstanding"].startswith(
            "reported diluted weighted-average shares over FY2025")
        assert "not the period-end count" in a.sources["shares_outstanding"]

    def test_a_basic_count_fallback_is_not_called_diluted(self):
        a = DCFModel()._infer_assumptions(self._kpis("CommonStockSharesOutstanding"))
        assert "diluted" not in a.sources["shares_outstanding"].split(" — ")[0]
        assert a.sources["shares_outstanding"].startswith(
            "reported period-end basic shares outstanding, FY2025")


# ---------------------------------------------------------------------------
# Horizon and share-count edge cases are refused, not papered over
# ---------------------------------------------------------------------------

def test_a_longer_horizon_than_the_growth_schedule_is_refused():
    """DCFModel(n_years=10).run(DCFAssumptions()) silently projected 5 years."""
    with pytest.raises(ValueError, match="A 10-year projection needs 10 revenue growth rates"):
        DCFModel(n_years=10).run(DCFAssumptions(), compute_sensitivity=False)


def test_the_horizon_is_n_years_when_the_schedule_covers_it():
    a = DCFAssumptions(revenue_growth_rates=[0.05] * 10)
    assert len(DCFModel(n_years=10).run(a, compute_sensitivity=False).projected_fcfs) == 10
    assert len(DCFModel(n_years=3).run(a, compute_sensitivity=False).projected_fcfs) == 3


@pytest.mark.parametrize("shares", [0, -5.0])
def test_a_non_positive_share_count_is_refused(shares):
    """A share count of zero is refused, not divided by."""
    with pytest.raises(ValueError, match="shares_outstanding must be positive"):
        DCFModel().run(DCFAssumptions(shares_outstanding=shares), compute_sensitivity=False)


# ---------------------------------------------------------------------------
# A margin below the floor refuses instead of being lifted
# ---------------------------------------------------------------------------

def _loss_maker(operating_income=-500.0, ebitda=-200.0):
    from finsight.extraction.kpi_extractor import ExtractedKPIs
    k = ExtractedKPIs(ticker="LOSS", period="FY2025", source_type="10-K",
                      extraction_method="xbrl")
    k.revenue, k.operating_income, k.ebitda = 10_000.0, operating_income, ebitda
    k.shares_outstanding, k.revenue_growth_yoy = 1_000.0, 0.10
    k.capex, k.total_debt, k.cash_and_equivalents = 500.0, 0.0, 0.0
    return k


@pytest.mark.parametrize("op_income,ebitda", [(-500.0, -200.0), (100.0, 300.0)])
def test_a_margin_below_the_floor_produces_no_value(op_income, ebitda):
    """An EBITDA margin under the floor (−2%, 3%) is refused, not lifted to 5% and valued."""
    from finsight.models.dcf import DCFInputError
    # A negative operating income refuses first, with its own reason; a positive but
    # thin margin still hits the floor.
    with pytest.raises(DCFInputError, match=r"below the 5% this model needs|operating loss"):
        DCFModel().value_from_kpis(_loss_maker(op_income, ebitda))


def test_a_margin_at_the_floor_is_valued_unfloored():
    a = DCFModel()._infer_assumptions(_loss_maker(300.0, 500.0))   # exactly 5%
    assert a.ebitda_margin == pytest.approx(0.05)
    assert "ebitda_margin" not in a.adjustments


def test_dcf_endpoint_answers_422_for_a_loss_maker(monkeypatch):
    from fastapi.testclient import TestClient
    import finsight.api.app as app_module

    monkeypatch.setattr(app_module.demo_data, "demo_enabled", lambda: False)
    monkeypatch.setattr(app_module, "_require_sec_listed", lambda t: None)
    monkeypatch.setattr(app_module, "_live_kpis", lambda t, period=None: _loss_maker())
    r = TestClient(app_module.app).get("/dcf/LOSS")
    assert r.status_code == 422 and "Not meaningful: operating income" in r.json()["detail"]


def test_sensitivity_cells_equal_the_endpoints_own_result():
    """Each cell fades growth to that cell's terminal rate, so the AAPL WACC 10% / TGR 4%
    cell is what /dcf/AAPL?terminal_growth=0.04 returns."""
    from finsight.data import companyfacts_snapshot
    from finsight.extraction.kpi_extractor import KPIExtractor

    k = KPIExtractor().extract_from_companyfacts(companyfacts_snapshot("AAPL"), "AAPL")
    table = DCFModel().value_from_kpis(k).sensitivity_table
    for wacc, g in [(0.10, 0.04), (0.09, 0.02), (0.12, 0.05), (0.10, 0.03)]:
        direct = DCFModel().value_from_kpis(k, wacc=wacc, terminal_growth=g)
        assert table.loc[f"WACC={wacc:.1%}", f"TGR={g:.1%}"] == pytest.approx(
            round(direct.intrinsic_value_per_share, 2))
    assert table.loc["WACC=10.0%", "TGR=4.0%"] == pytest.approx(191.01)



# ---------------------------------------------------------------------------
# No placeholder margin; assumed fundamentals are named
# ---------------------------------------------------------------------------

def _untagged(**kw):
    from finsight.extraction.kpi_extractor import ExtractedKPIs
    base = dict(ticker="X", period="FY2025", source_type="10-K", revenue=10_000.0,
                net_income=-3_000.0, shares_outstanding=1_000.0, revenue_growth_yoy=0.05)
    return ExtractedKPIs(**{**base, **kw})


def test_a_missing_margin_refuses_instead_of_assuming_20_percent():
    """A net loss with no operating-income tag is refused, not valued at a default margin."""
    from finsight.models.dcf import DCFInputError
    with pytest.raises(DCFInputError, match="neither operating income nor EBITDA"):
        DCFModel().value_from_kpis(_untagged())


@pytest.mark.parametrize("omitted", [{}, {"ebitda": None}, {"operating_income": None, "ebitda": None}])
def test_a_loss_maker_cannot_get_a_value_by_omitting_a_tag(omitted):
    from finsight.models.dcf import DCFInputError
    k = _untagged(**{"operating_income": -2_500.0, "ebitda": -2_000.0, **omitted})
    with pytest.raises(DCFInputError):
        DCFModel().value_from_kpis(k)


def test_assumed_fundamentals_are_named_beside_the_value():
    from finsight.api.payloads import dcf_payload
    from finsight.report.generator import ReportGenerator

    k = _untagged(net_income=1_500.0, operating_income=2_000.0)   # no D&A, CapEx, debt, cash
    result = DCFModel().value_from_kpis(k)
    assumed = ["capex_as_pct_revenue", "da_as_pct_revenue", "net_debt"]
    assert result.assumptions.assumed_fundamentals == assumed
    assert dcf_payload("X", k, result, 5, mode="live")["assumed_fundamentals"] == assumed
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate(ticker="X", kpis=k, dcf_result=result, current_price=50.0))
    assert ("It also rests on figures the filing did not report, which the model "
            "assumed: capex_as_pct_revenue, da_as_pct_revenue, net_debt") in md


def test_no_demo_value_rests_on_an_assumed_fundamental():
    from finsight.api import demo_data
    for t in demo_data.DEMO_TICKERS:
        assert demo_data.demo_dcf(t)["assumed_fundamentals"] == [], t


# ---------------------------------------------------------------------------
# Acquisition amortization is not reinvestment
# ---------------------------------------------------------------------------

def _acquirer():
    """Broadcom-like: D&A is mostly amortization of acquired intangibles."""
    from finsight.extraction.kpi_extractor import KPIExtractor
    end = "2025-11-02"

    def fact(v):
        return [{"end": end, "val": v, "unit": "USD", "accn": "A-1", "filed": "2025-12-18",
                 "first_accn": "A-1", "first_filed": "2025-12-18", "first_fy": 2025}]
    series = {
        "Revenues": fact(50_000e6), "OperatingIncomeLoss": fact(15_000e6),
        "DepreciationDepletionAndAmortization": fact(10_200e6),
        "Depreciation": fact(550e6), "AmortizationOfIntangibleAssets": fact(9_650e6),
        "PaymentsToAcquirePropertyPlantAndEquipment": fact(550e6),
        "WeightedAverageNumberOfDilutedSharesOutstanding": [
            {"end": end, "val": 4_900e6, "unit": "shares", "accn": "A-1", "filed": "2025-12-18"}],
    }
    return KPIExtractor().extract_from_xbrl(series, "ACQR")


def test_capex_fades_to_tangible_reinvestment_not_acquisition_amortization():
    """Steady-state CapEx leaves acquisition amortization out: forcing 1.1% of revenue to 20.4% (total D&A) would
    roughly halve the value."""
    k = _acquirer()
    assert (k.depreciation, k.amortization_of_intangibles) == (550.0, 9_650.0)
    result = DCFModel().value_from_kpis(k)
    a = result.assumptions
    assert a.da_as_pct_revenue == pytest.approx(0.204)            # the add-back stays total
    assert a.terminal_capex_as_pct_revenue == pytest.approx(0.011)
    assert result.projected_capex_pct[-1] == pytest.approx(0.011)
    assert "Steady-state CapEx is D&A less amortization of acquired intangibles: 1.1%" \
        in a.limitations["steady_state_capex"]
    import dataclasses
    old_way = DCFModel().run(dataclasses.replace(a, terminal_capex_as_pct_revenue=0.204),
                             compute_sensitivity=False)
    assert result.intrinsic_value_per_share > 1.5 * old_way.intrinsic_value_per_share


def test_a_combined_da_is_conservative_and_says_so():
    from finsight.api import demo_data
    dcf = demo_data.demo_dcf("AAPL", period="2023")
    note = dcf["limitations"]["steady_state_capex"]
    assert note.startswith("Steady-state CapEx is the combined D&A rate (3.0%)")
    assert "depreciation of property and equipment is 2.2% of revenue" in note
    assert dcf["assumptions"]["terminal_capex_as_pct_revenue"] == pytest.approx(
        dcf["assumptions"]["da_as_pct_revenue"])


def test_a_separately_reported_amortization_leaves_the_steady_state():
    from finsight.api import demo_data
    for ticker, rate, steady in (("AAPL", "2.8%", "1.8%"), ("MSFT", "11.8%", "10.3%"), ("NVDA", "1.3%", "1.1%")):
        dcf = demo_data.demo_dcf(ticker)
        note = dcf["limitations"]["steady_state_capex"]
        assert f"{steady} of revenue, not the {rate} D&A rate" in note
        assert dcf["assumptions"]["terminal_capex_as_pct_revenue"] < dcf["assumptions"]["da_as_pct_revenue"]


@pytest.mark.parametrize("ticker,value", [("AAPL", 165.3101), ("MSFT", 313.2336),
                                          ("NVDA", 127.4405), ("GOOGL", 142.6528),
                                          ("TSLA", 13.1944)])
def test_demo_values_do_not_move(ticker, value):
    """The documented value of each bundled snapshot (cut 2026-11-04)."""
    from finsight.api import demo_data
    assert demo_data.demo_dcf(ticker)["intrinsic_value_per_share"] == pytest.approx(value, abs=5e-5)



# ---------------------------------------------------------------------------
# Operating losses and non-positive equity refuse; no tax credit
# ---------------------------------------------------------------------------

def test_an_operating_loss_with_a_healthy_ebitda_margin_refuses():
    """An operating loss is refused even with a healthy EBITDA margin (−1.5% EBIT, 18% EBITDA)."""
    from finsight.models.dcf import DCFInputError
    k = _untagged(operating_income=-150.0, ebitda=1_800.0)
    with pytest.raises(DCFInputError, match="Not meaningful: operating income is \\$-150M"):
        DCFModel().value_from_kpis(k)


def test_a_non_positive_equity_value_refuses():
    from finsight.models.dcf import DCFInputError
    k = _untagged(operating_income=1_000.0, ebitda=1_200.0, total_debt=1_000_000.0,
                  cash_and_equivalents=0.0)
    with pytest.raises(DCFInputError, match="Not meaningful: the DCF equity value is"):
        DCFModel().value_from_kpis(k)


def test_a_loss_never_earns_a_tax_credit():
    a = DCFAssumptions(ebitda_margin=0.05, da_as_pct_revenue=0.10, capex_as_pct_revenue=0.10,
                       revenue_growth_rates=[0.0] * 5, nwc_pct_of_revenue_change=0.0)
    # EBIT = −5% of revenue: FCF = EBIT + D&A − CapEx = −5%, untaxed (not −3.95%).
    assert DCFModel._fcf(1_000.0, 1_000.0, a, 0.10) == pytest.approx(-50.0)


def test_report_header_and_body_agree_on_a_non_positive_value():
    from finsight.report.generator import ReportGenerator
    result = DCFModel().run(DCFAssumptions(net_debt=1e9), compute_sensitivity=False)
    assert result.intrinsic_value_per_share < 0
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate(ticker="X", dcf_result=result, current_price=50.0))
    assert "| Not meaningful (equity value ≤ 0) | $50.00 |" in md
    assert "- **Intrinsic Value per Share (model):** Not meaningful (equity value ≤ 0)" in md
    assert "$-" not in md


def test_the_docstring_examples_run():
    """DCFModel's example lacked a margin input and raised DCFInputError."""
    import doctest
    import finsight.models.dcf as dcf_module
    result = doctest.testmod(dcf_module, verbose=False)
    assert result.attempted > 0 and result.failed == 0


class TestTwoFreeCashFlows:
    """The DCF's FCF and the reported one share a name and are different figures."""

    @pytest.fixture
    def kpis(self):
        from finsight.extraction.kpi_extractor import ExtractedKPIs
        return ExtractedKPIs(ticker="X", period="FY2025", source_type="10-K", revenue=10_000,
                             operating_income=2_000, ebitda=2_500, capex=400,
                             free_cash_flow=1_750, shares_outstanding=1_000,
                             revenue_growth_yoy=0.05)

    def test_a_limitation_says_what_each_one_is(self, kpis):
        note = DCFModel().value_from_kpis(kpis).assumptions.limitations["free_cash_flow_basis"]
        assert "EBIT × (1 − tax) + D&A − CapEx − ΔNWC" in note
        assert "stock-based compensation stays a cost and is never added back" in note
        assert "operating cash flow − CapEx, $1,750M for FY2025" in note
        # 2,000 × 0.79 + 500 − 400, by hand.
        assert "before growth and ΔNWC, gives $1,680M" in note

    def test_a_negative_figure_has_its_sign_before_the_dollar(self, kpis):
        kpis.capex = 2_500
        note = DCFModel().value_from_kpis(kpis).assumptions.limitations["free_cash_flow_basis"]
        assert "gives −$420M" in note
        assert "$-" not in note

    def test_an_unreported_figure_is_not_read_as_zero(self, kpis):
        kpis.free_cash_flow = None
        note = DCFModel().value_from_kpis(kpis).assumptions.limitations["free_cash_flow_basis"]
        assert "not available for FY2025" in note


class TestANegativeEnterpriseValueIsNotAValuation:
    """Net cash left a positive value on a business modelled at less than nothing:
    intrinsic=$49.12, EV=$-0.1B, TV%=0.0%, at a 200."""

    @staticmethod
    def _kpis(**over):
        from finsight.extraction.kpi_extractor import ExtractedKPIs
        fields = dict(ticker="X", period="FY2025", source_type="10-K", revenue=10_000,
                      operating_income=1, ebitda=1_000, capex=2_000, shares_outstanding=100,
                      revenue_growth_yoy=0.05, total_debt=0, cash_and_equivalents=5_000)
        return ExtractedKPIs(**{**fields, **over})

    def test_it_is_refused_with_the_reason(self):
        from finsight.models.dcf import DCFInputError
        with pytest.raises(DCFInputError, match="Not meaningful: the DCF enterprise value is \\$-"):
            DCFModel().value_from_kpis(self._kpis())

    def test_a_positive_enterprise_value_with_net_cash_still_values(self):
        result = DCFModel().value_from_kpis(self._kpis(operating_income=2_000, ebitda=2_500, capex=400))
        assert result.enterprise_value > 0 and result.equity_value > result.enterprise_value

    def test_the_equity_reason_still_comes_first_when_both_fail(self):
        from finsight.models.dcf import DCFInputError
        with pytest.raises(DCFInputError, match="the DCF equity value is"):
            DCFModel().value_from_kpis(self._kpis(cash_and_equivalents=0, total_debt=5_000))

    def test_the_endpoint_answers_422_and_the_page_shows_why(self, monkeypatch):
        from fastapi.testclient import TestClient
        import finsight.api.app as app_module

        monkeypatch.setenv("FINSIGHT_DEMO", "1")
        monkeypatch.setattr(app_module.demo_data, "_kpis_for", lambda t, p=None: self._kpis())
        r = TestClient(app_module.app).get("/dcf/AAPL")
        assert r.status_code == 422
        assert r.json()["detail"].startswith("Not meaningful: the DCF enterprise value")
