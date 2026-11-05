"""
An independent, written-out derivation of FinSight's documented DCF method: it checks
the engine's numbers (and the documented demo value) without trusting the engine to
check itself.
"""


def hand_dcf(revenue, prior_revenue, operating_income, d_and_a, capex, debt, cash,
             shares, wacc=0.10, g=0.03, n=5, nwc=0.02, tax=0.21, amortization=0):
    """The documented method, written out independently of DCFModel.

    Year-1 growth = trailing growth (inside −30%…+40%), linear to g by year n;
    EBITDA margin = (OI + D&A) / revenue; D&A as % of revenue, held flat;
    CapEx as % of revenue = the REPORTED rate in year 1, moving in equal steps
    to the steady-state rate by year n, and that rate in the terminal year, where
    steady state = (D&A − amortization of acquired intangibles) / revenue;
    FCF = (EBITDA − D&A)(1 − tax) + D&A − CapEx − nwc × ΔRevenue; Gordon
    terminal value on year n+1 growing at g.
    """
    g1 = min(max(revenue / prior_revenue - 1, -0.30), 0.40)
    growth = [g1 + (g - g1) * i / (n - 1) for i in range(n)]
    margin = (operating_income + d_and_a) / revenue
    da = d_and_a / revenue
    cx1 = capex / revenue
    steady = (d_and_a - amortization) / revenue
    capex_rate = [cx1 + (steady - cx1) * i / (n - 1) for i in range(n)]

    def fcf(rev, prev, cx):
        return (rev * margin - rev * da) * (1 - tax) + rev * da - rev * cx - nwc * (rev - prev)

    pv, prev = 0.0, revenue
    for year, (rate, cx) in enumerate(zip(growth, capex_rate), start=1):
        rev = prev * (1 + rate)
        pv += fcf(rev, prev, cx) / (1 + wacc) ** year
        prev = rev
    terminal = fcf(prev * (1 + g), prev, steady) / (wacc - g)
    ev = pv + terminal / (1 + wacc) ** n
    return (ev - (debt - cash)) / shares


# Apple FY2026 10-K (fiscal year ended 2026-09-26, accession 0000320193-26-000026), $M.
APPLE_FY2026 = dict(
    revenue=478_834, prior_revenue=416_161, operating_income=158_063, d_and_a=13_423,
    amortization=4_900, capex=9_041, debt=78_114 + 1_997, cash=36_305, shares=14_720.306,
)

# Microsoft FY2026 10-K (fiscal year ended 2026-06-30, accession
# 0001193125-26-323660), $M. D&A is `Depreciation` + `AmortizationOfIntangibleAssets`;
# CapEx is 34.9% of revenue against a steady state of 10.3%, which is what makes the
# CapEx method matter.
MICROSOFT_FY2026 = dict(
    revenue=331_839, prior_revenue=281_724, operating_income=155_237, d_and_a=34_300 + 4_700,
    amortization=4_700, capex=115_948, debt=40_294, cash=20_935, shares=7_453,
)

# Alphabet FY2025 10-K (fiscal year ended 2025-12-31, accession
# 0001652044-26-000018), $M. CapEx 22.7% of revenue against D&A of 5.2%.
ALPHABET_FY2025 = dict(
    revenue=402_836, prior_revenue=350_018, operating_income=129_039, d_and_a=21_136,
    capex=91_447, debt=49_085, cash=30_708, shares=12_230,
)


def dollars_at_year_end_shares(value_per_share, diluted, year_end):
    """The same equity value, divided by the other share count."""
    return value_per_share * diluted / year_end
