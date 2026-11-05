"""Tests for sentiment analysis."""

import pytest
from finsight.sentiment.analyzer import SentimentAnalyzer, SentimentResult


POSITIVE_TEXT = """
Revenue grew 25% year-over-year to record levels, driven by strong AI demand.
We achieved excellent gross margins and delivered outstanding free cash flow.
Our innovative products continue to win market share. We are optimistic about
our expanding opportunities in cloud and AI segments.
"""

NEGATIVE_TEXT = """
Revenue declined significantly due to challenging macroeconomic headwinds.
We faced difficult competition and disappointing customer losses. Our losses
widened as inflation pressured margins. Uncertainty around demand remains
our primary concern. We are reducing our guidance due to these challenges.
"""

NEUTRAL_TEXT = "The company reported quarterly results. Revenue was flat."


@pytest.fixture
def analyzer():
    return SentimentAnalyzer(use_finbert=False)


class TestSentimentAnalyzer:
    def test_positive_text_positive_sentiment(self, analyzer):
        result = analyzer.analyze_text(POSITIVE_TEXT, "test")
        assert result.net_sentiment > 0
        assert result.polarity == "positive"

    def test_negative_text_negative_sentiment(self, analyzer):
        result = analyzer.analyze_text(NEGATIVE_TEXT, "test")
        assert result.net_sentiment < 0
        assert result.polarity == "negative"

    def test_neutral_text_neutral_sentiment(self, analyzer):
        result = analyzer.analyze_text(NEUTRAL_TEXT, "test")
        # Neutral text should have low absolute sentiment
        assert abs(result.net_sentiment) < 0.15

    def test_result_has_required_fields(self, analyzer):
        result = analyzer.analyze_text(POSITIVE_TEXT, "source")
        assert isinstance(result, SentimentResult)
        assert result.positive_score >= 0
        assert result.negative_score >= 0
        assert result.word_count > 0
        assert result.text_source == "source"

    def test_scores_are_the_word_fractions(self, analyzer):
        # Replaces "positive + negative <= 1.5", which no input could fail
        #: each score is exactly its tone words / all words.
        result = analyzer.analyze_text("Results were strong but demand was weak and poor.", "test")
        assert result.word_count == 9
        assert result.positive_score == pytest.approx(1 / 9)    # strong
        assert result.negative_score == pytest.approx(2 / 9)    # weak, poor
        assert result.net_sentiment == pytest.approx(-1 / 9)

    def test_empty_text(self, analyzer):
        result = analyzer.analyze_text("", "empty")
        assert result.word_count == 0
        assert result.net_sentiment == 0

    def test_batch_analyze(self, analyzer):
        texts = [POSITIVE_TEXT, NEGATIVE_TEXT, NEUTRAL_TEXT]
        results = analyzer.batch_analyze(texts, ["pos", "neg", "neu"])
        assert len(results) == 3
        assert results[0].net_sentiment > results[1].net_sentiment

    def test_transcript_analysis(self, analyzer):
        from finsight.ingestion.transcripts import TranscriptFetcher
        fetcher = TranscriptFetcher(cache=False)
        transcript = fetcher.create_mock_transcript("AAPL", "Q4 2023")
        result = analyzer.analyze_transcript(transcript)
        assert result.ticker == "AAPL"
        assert result.quarter == "Q4 2023"
        assert result.overall is not None
        from finsight.sentiment.analyzer import GUIDANCE_TONES
        assert result.guidance_tone in GUIDANCE_TONES

    def test_guidance_detection_raised(self, analyzer):
        text = "We are raising our guidance for revenue to $15B for next quarter."
        transcript_mock = type("T", (), {
            "ticker": "X", "quarter": "Q1", "full_text": text,
            "prepared_remarks": text, "qa_section": ""
        })()
        result = analyzer.analyze_transcript(transcript_mock)
        assert result.guidance_tone == "raised"

    def test_guidance_detection_lowered(self, analyzer):
        text = "We are lowering our guidance due to headwinds."
        transcript_mock = type("T", (), {
            "ticker": "X", "quarter": "Q1", "full_text": text,
            "prepared_remarks": text, "qa_section": ""
        })()
        result = analyzer.analyze_transcript(transcript_mock)
        assert result.guidance_tone == "lowered"

    def test_sentiment_trend_direction(self, analyzer):
        # Create fake transcripts with improving sentiment over time.
        # We need objects that expose ``overall.net_sentiment`` and ``quarter``.
        from types import SimpleNamespace

        transcripts = []
        for i in range(4):
            overall = SimpleNamespace(net_sentiment=-0.05 + i * 0.03)
            ts = SimpleNamespace(overall=overall, quarter=f"Q{i+1} 2023")
            transcripts.append(ts)

        trend = analyzer.compute_sentiment_trend(transcripts)
        assert "net_sentiment_series" in trend
        assert "trend_direction" in trend
        assert trend["trend_direction"] == "improving"


# ---------------------------------------------------------------------------
# Key phrases must actually lean the way they are labelled
# ---------------------------------------------------------------------------

import re  # noqa: E402



def _hits(sentence: str) -> tuple[int, int]:
    from finsight.sentiment.analyzer import NEGATIVE, POSITIVE, _lexicon_hits

    words = re.findall(r"\b[a-z]+\b", sentence.lower())
    return _lexicon_hits(words, POSITIVE), _lexicon_hits(words, NEGATIVE)


class TestKeyPhrases:
    """Only a sentence with a negative word can be a key concern: "Free cash flow was $3.1 billion" is not."""

    MIXED = (
        "Revenue declined 5% on weak demand. Free cash flow was $3.1 billion. "
        "Margins improved despite headwinds. We returned $1.5 billion to "
        "shareholders. Growth was strong in cloud."
    )

    def test_a_concern_is_net_negative_and_a_positive_net_positive(self, analyzer):
        result = analyzer.analyze_text(self.MIXED, "test")
        assert result.key_negative_phrases == ["Revenue declined 5% on weak demand"]
        assert result.key_positive_phrases == ["Growth was strong in cloud."]
        for phrase in result.key_negative_phrases:
            pos, neg = _hits(phrase)
            assert neg >= 1 and neg > pos
        for phrase in result.key_positive_phrases:
            pos, neg = _hits(phrase)
            assert pos >= 1 and pos > neg

    def test_no_negative_sentence_means_no_concerns(self, analyzer):
        result = analyzer.analyze_text(
            "Free cash flow was $3.1 billion. We returned $1.5 billion to shareholders.",
            "test")
        assert result.key_negative_phrases == []
        assert result.key_positive_phrases == []

    # The property the two tests below pin: a listed phrase leans the way it is labelled.
    def test_demo_aapl_placeholder_lists_only_leaning_sentences(self):
        from finsight.ingestion.transcripts import TranscriptFetcher

        transcript = TranscriptFetcher(cache=False).create_mock_transcript("AAPL", "Q4 (synthetic)")
        overall = SentimentAnalyzer().analyze_transcript(transcript).overall
        assert overall.key_negative_phrases == [
            "Speaker A: Sample remark: demand was weak and costs created pressure"]
        assert overall.key_positive_phrases == [
            "Speaker A: Sample remark: results were strong and margins improved"]
        assert overall.net_sentiment == 0 and overall.sentiment_label == "Neutral"
        for phrase in overall.key_negative_phrases:
            pos, neg = _hits(phrase)
            assert neg > pos

    def test_an_empty_list_says_none_found(self):
        from types import SimpleNamespace
        from finsight.report.generator import ReportGenerator

        overall = SimpleNamespace(sentiment_label="Neutral", net_sentiment=0.0,
                                  key_positive_phrases=[], key_negative_phrases=[])
        gen = ReportGenerator()
        md = gen.to_markdown(gen.generate(ticker="X", sentiment=SimpleNamespace(
            overall=overall, guidance_tone="not_stated", risk_mentions=0, sentiment_drift=None)))
        concerns = md[md.index("**Key Concerns:**"):].split("\n\n")[0]
        assert concerns == (
            "**Key Concerns:** none found — no sentence scores net-negative on "
            "the sentiment lexicon.")
        assert "**Key Positive Statements:** none found" in md


# ---------------------------------------------------------------------------
# The word lists carry tone, not direction
# ---------------------------------------------------------------------------

class TestWordLists:
    """The lists hold no direction words ("increased", "higher", "growth", "earnings", "significant"), so a sentence
    about growing losses does not read Bullish, and only a `stem*` entry matches by prefix."""

    def test_growing_losses_are_not_bullish(self, analyzer):
        r = analyzer.analyze_text(
            "Net losses increased significantly and operating expenses grew "
            "higher than expected.", "test")
        assert r.sentiment_label == "Bearish"
        assert r.net_sentiment == pytest.approx(-1 / 11)  # "losses" is the only tone word

    def test_a_plain_statement_of_results_is_neutral(self, analyzer):
        r = analyzer.analyze_text(
            "Revenue was $10.2 billion and operating expenses were $3.1 billion.", "test")
        assert r.net_sentiment == 0 and r.sentiment_label == "Neutral"

    @pytest.mark.parametrize("word", [
        "earnings", "growth", "grew", "increased", "increase", "higher", "significant",
        "significantly", "expanded", "record", "value", "lower", "declined", "decrease",
        "reduced", "inflation", "momentum", "delivered",
    ])
    def test_direction_and_call_vocabulary_carry_no_tone(self, analyzer, word):
        r = analyzer.analyze_text(f"The {word} figure.", "test")
        assert r.positive_score == 0 and r.negative_score == 0

    @pytest.mark.parametrize("word", ["will", "expect", "expected", "guidance", "if", "would",
                                      "estimate", "project", "projected", "certain"])
    def test_ordinary_words_are_not_uncertainty(self, analyzer, word):
        assert analyzer.analyze_text(f"The {word} figure.", "test").uncertainty_score == 0

    @pytest.mark.parametrize("word", ["confidential", "progressive", "winnings",
                                      "benefitted", "strongholds"])
    def test_no_accidental_prefix_hits(self, analyzer, word):
        assert analyzer.analyze_text(f"The {word} figure.", "test").positive_score == 0

    def test_only_starred_entries_match_by_prefix(self, analyzer):
        from finsight.sentiment.analyzer import NEGATIVE, WordList

        for word in ("deteriorating", "deterioration", "impairments", "disappointing"):
            assert word in NEGATIVE
        assert analyzer.analyze_text("Margins are deteriorating.", "test").negative_score > 0
        wl = WordList({"gain", "weak*"})
        assert wl.words == {"gain"} and wl.stems == ("weak",)
        assert "gains" not in wl and "weakening" in wl

    def test_the_lists_are_not_called_loughran_mcdonald(self):
        import finsight.sentiment.analyzer as module
        assert "inspired by" in module.__doc__ and "not** the LM dictionary" in module.__doc__


# ---------------------------------------------------------------------------
# Guidance tone = an explicit action on guidance
# ---------------------------------------------------------------------------

class TestGuidanceTone:
    """"above/below ... guidance" (results against guidance) is not an action, and a mention of guidance is not one either."""

    @pytest.mark.parametrize("text,tone", [
        # The review's two sentences.
        ("Revenue came in above our guidance this quarter.", "not_stated"),
        ("Revenue came in below guidance this quarter; we reaffirm our full-year outlook.",
         "maintained"),
        # Results against guidance are not a change.
        ("Revenue increased 8%, in line with our guidance.", "not_stated"),
        ("Results exceeded our forecast.", "not_stated"),
        ("Revenue came in at the lower end of our guidance range.", "not_stated"),
        ("Guidance for next quarter: revenue of $10.5 to $10.8 billion.", "not_stated"),
        # Explicit actions.
        ("We are raising our full-year guidance.", "raised"),
        ("We lowered our full-year outlook.", "lowered"),
        ("Our full-year guidance was raised to $12 billion.", "raised"),
        ("We raised our revenue guidance but cut our margin outlook.", "mixed"),
        ("We reaffirm our outlook for the year.", "maintained"),
        ("Our full-year outlook is unchanged.", "maintained"),
        ("We are initiating guidance for fiscal 2027.", "initiated"),
        ("Given the uncertainty, we are withdrawing our full-year guidance.", "withdrawn"),
        ("Revenue was $10 billion.", "not_stated"),
    ])
    def test_only_an_explicit_action_counts(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone


# ---------------------------------------------------------------------------
# FinBERT: separate scale, safe truncation, visible failures.
# The pipeline is ALWAYS stubbed: no model is downloaded by these tests.
# ---------------------------------------------------------------------------

class _FakeFinBERT:
    def __init__(self, fail_on: set[int] | None = None):
        self.calls: list[tuple[int, dict]] = []
        self.fail_on = fail_on or set()

    def __call__(self, chunk, **kwargs):
        self.calls.append((len(chunk), kwargs))
        if len(self.calls) in self.fail_on:
            raise RuntimeError("token indices sequence length is longer than the maximum")
        return [[{"label": "positive", "score": 0.9}, {"label": "negative", "score": 0.05},
                 {"label": "neutral", "score": 0.05}]]


def _with_finbert(pipe, **kw):
    a = SentimentAnalyzer(use_finbert=False, **kw)   # never loads a real model
    a.use_finbert, a._finbert_pipeline = True, pipe
    return a


class TestFinBERT:
    TEXT = "Margins improved. Demand was weak. Revenue was $10 billion."

    def test_finbert_does_not_overwrite_the_word_list_score(self):
        plain = SentimentAnalyzer().analyze_text(self.TEXT)
        r = _with_finbert(_FakeFinBERT()).analyze_text(self.TEXT)
        # The old blend made this 0.6 × 0.85 + 0.4 × 0 = 0.51.
        assert r.net_sentiment == plain.net_sentiment == 0
        assert r.finbert_net == pytest.approx(0.85)
        # Word-list tone (1 positive − 1 negative) / 2 = 0 → blend = 0.6 × 0.85.
        assert r.blended_tone == pytest.approx(0.51)
        assert r.finbert_status == "ok" and r.finbert_chunks == 1

    def test_blend_puts_the_word_lists_on_the_same_scale(self):
        r = _with_finbert(_FakeFinBERT()).analyze_text("Results were strong. Margins improved.")
        assert r.blended_tone == pytest.approx(0.6 * 0.85 + 0.4 * 1.0)

    def test_every_chunk_is_truncated_safely_and_none_silently_dropped(self):
        pipe = _FakeFinBERT()
        r = _with_finbert(pipe).analyze_text("word " * 2400)   # 12,000 chars = 6 chunks
        assert r.finbert_chunks == 6 and len(pipe.calls) == 6
        assert all(kw.get("truncation") is True and kw.get("max_length") == 512
                   for _, kw in pipe.calls)

    def test_chunks_past_the_cap_are_reported(self):
        r = _with_finbert(_FakeFinBERT(), max_chunks=2).analyze_text("word " * 2400)
        assert r.finbert_chunks == 2
        assert r.finbert_status == "ok; 4 chunk(s) past the first 2 not scored"

    def test_a_failed_chunk_is_a_warning_and_a_status_not_a_silent_zero(self, caplog):
        r = _with_finbert(_FakeFinBERT(fail_on={1})).analyze_text("word " * 800)  # 2 chunks
        assert r.finbert_status == "partial: 1 of 2 chunks failed"
        assert r.finbert_net == pytest.approx(0.85)            # from the chunk that worked
        assert any(rec.levelname == "WARNING" and "FinBERT chunk 1/2 failed" in rec.message
                   for rec in caplog.records)

    def test_all_chunks_failing_leaves_no_finbert_score(self):
        r = _with_finbert(_FakeFinBERT(fail_on={1, 2, 3})).analyze_text(self.TEXT)
        assert r.finbert_status.startswith("failed: no chunk could be scored (RuntimeError)")
        assert r.finbert_score is None and r.finbert_net is None and r.blended_tone is None

    def test_a_missing_transformers_install_is_surfaced(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "transformers", None)   # import raises ImportError
        r = SentimentAnalyzer(use_finbert=True).analyze_text(self.TEXT)
        assert r.finbert_status == "unavailable: transformers is not installed"
        assert r.finbert_score is None

    def test_not_requested_means_no_status(self):
        assert SentimentAnalyzer().analyze_text(self.TEXT).finbert_status is None


class TestGuidanceActionIsGovernedAndAffirmed:
    """The verb must take the guidance phrase as its object, in an affirmed clause."""

    @pytest.mark.parametrize("text,tone", [
        # The four repro sentences ...
        ("We are not raising our guidance.", "not_stated"),
        ("We have no plans to withdraw guidance.", "not_stated"),
        ("We reduced costs and raised our full-year guidance.", "raised"),
        ("Revenue increased 8% and we reaffirm our outlook.", "maintained"),
        # ... and their affirmed / governed counterparts.
        ("We are raising our guidance.", "raised"),
        # A plan is not an action.
        ("We plan to withdraw guidance.", "not_stated"),
        ("We are withdrawing our guidance.", "withdrawn"),
        ("We reduced our full-year guidance.", "lowered"),
        ("Revenue increased 8% and we raised our outlook.", "raised"),
        # More negation and hypotheticals.
        ("We did not lower our outlook.", "not_stated"),
        ("We won't change our outlook.", "not_stated"),
        ("We do not expect to lower guidance.", "not_stated"),
        ("Analysts asked whether we would raise guidance; we did not.", "not_stated"),
        # A verb whose object is something else.
        ("We cut operating expenses and issued guidance for 2026.", "initiated"),
        ("Sales increased and we reiterate our guidance.", "maintained"),
    ])
    def test_classification(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone

    def test_the_report_does_not_assert_a_negated_action(self):
        from types import SimpleNamespace
        from finsight.report.generator import ReportGenerator

        t = SimpleNamespace(ticker="X", quarter="Q1", full_text="We are not raising our guidance.",
                            prepared_remarks="We are not raising our guidance.", qa_section="")
        s = SentimentAnalyzer().analyze_transcript(t)
        gen = ReportGenerator()
        report = gen.generate(ticker="X", sentiment=s, sentiment_synthetic=False)
        assert "raised guidance" not in report.investment_thesis
        assert "Management raised forward guidance" not in report.key_highlights



class TestOnlyRealisedDeclarativeActionsCount:
    """Modals, conditionals, intent and questions are not actions."""

    @pytest.mark.parametrize("text", [
        "It is too early to raise guidance.",
        "We may raise our guidance later this year.",
        "We might lower our outlook.",
        "We could raise guidance.",
        "We would raise our guidance if demand holds.",
        "We'd raise our guidance if demand holds.",
        "We raise guidance if demand holds.",
        "We will raise guidance next quarter.",
        "We plan to raise our guidance.",
        "We hope to raise our outlook.",
        "We are considering raising our guidance.",
        "We are going to raise guidance.",
        "Are you going to raise guidance?",
        "Why did you lower guidance?",
        "Did you cut your outlook",
        "Analyst: Will you reaffirm the outlook?",
    ])
    def test_these_state_no_action(self, analyzer, text):
        assert analyzer._detect_guidance_tone(text) == "not_stated"

    @pytest.mark.parametrize("text,tone", [
        ("We are raising our guidance.", "raised"),
        ("We're raising our guidance.", "raised"),
        ("We raised our full-year guidance.", "raised"),
        ("We lowered our full-year outlook.", "lowered"),
        ("We now expect full-year revenue above our prior guidance.", "raised"),
        ("We now expect revenue below our previous outlook.", "lowered"),
        ("We reaffirm our outlook.", "maintained"),
        ("Our full-year outlook is unchanged.", "maintained"),
        ("We are initiating guidance for fiscal 2027.", "initiated"),
        ("Given the uncertainty, we are withdrawing our full-year guidance.", "withdrawn"),
    ])
    def test_realised_actions_still_count(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone

    @pytest.mark.parametrize("text", ["We may raise our guidance later this year.",
                                      "Are you going to raise guidance?"])
    def test_the_report_asserts_no_unfound_action(self, text):
        from types import SimpleNamespace
        from finsight.report.generator import ReportGenerator

        t = SimpleNamespace(ticker="X", quarter="Q1", full_text=text, prepared_remarks=text,
                            qa_section="")
        report = ReportGenerator().generate(
            ticker="X", sentiment=SentimentAnalyzer().analyze_transcript(t),
            sentiment_synthetic=False)
        assert "guidance" not in report.investment_thesis
        assert "Management raised forward guidance" not in report.key_highlights


# ---------------------------------------------------------------------------
# Tone negation; guidance phrasing
# ---------------------------------------------------------------------------

class TestToneNegation:
    """"Demand was not strong" is Bearish: negation counts."""

    @pytest.mark.parametrize("text,label", [
        ("Demand was not strong.", "Bearish"),
        ("Margins aren't improving.", "Bearish"),
        ("We never achieved our targets.", "Bearish"),
        ("There were no losses and no impairments.", "Neutral"),   # a negated negative is not praise
        ("Results were strong.", "Bullish"),                        # control
    ])
    def test_negated_tone_words(self, analyzer, text, label):
        assert analyzer.analyze_text(text, "t").sentiment_label == label

    def test_the_phrase_level_limitation_is_documented(self):
        """A word list cannot read "Losses narrowed": the README says so."""
        from pathlib import Path
        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        assert '"Losses narrowed significantly" scores Bearish' in readme
        assert SentimentAnalyzer().analyze_text("Losses narrowed significantly.").sentiment_label == "Bearish"


class TestGuidancePhrasing:
    @pytest.mark.parametrize("text,tone", [
        ("Guidance for the full year is unchanged.", "maintained"),
        ("Our outlook for fiscal 2026 was raised.", "raised"),
        ("Revenue rose 5%, reaffirming our guidance.", "maintained"),
        ("We delivered record results, reaffirming guidance.", "maintained"),
        ("We raised capex guidance.", "not_stated"),          # not revenue/earnings guidance
        ("We raised our capital spending outlook.", "not_stated"),
        ("We raised our revenue guidance.", "raised"),        # control
    ])
    def test_classification(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone


class TestGuidanceIsTheCompanys:
    """Other parties' actions and adjectives are not the company's."""

    @pytest.mark.parametrize("text", [
        "Analysts raised their forecasts for us after the quarter.",
        "Competitors have lowered their guidance.",
        "The stock fell on lower guidance.",
        "Peers cut their outlook.",
    ])
    def test_not_the_companys_action(self, analyzer, text):
        assert analyzer._detect_guidance_tone(text) == "not_stated"

    @pytest.mark.parametrize("text,tone", [
        ("We've raised guidance.", "raised"),
        ("Management raised the full-year outlook.", "raised"),
        ("Revenue rose 5%, reaffirming our guidance.", "maintained"),
        ("The company lowered its guidance.", "lowered"),
        ("Our guidance is unchanged.", "maintained"),
    ])
    def test_the_companys_action_counts(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone


class TestGuidanceOwnExamples:
    """Phrasings the classifier's own rules should settle."""

    @pytest.mark.parametrize("text,tone", [
        ("We are pleased to raise our full-year guidance.", "raised"),   # announced, not intended
        ("We reduced our forecast for capital expenditures.", "not_stated"),  # not revenue guidance
        ("We issued second-quarter guidance.", "not_stated"),           # routine quarterly
        ("We issued guidance for the second quarter.", "not_stated"),
        ("We reduced our full-year forecast.", "lowered"),              # control
        ("We are initiating guidance for fiscal 2027.", "initiated"),   # control
        ("We plan to raise guidance.", "not_stated"),                   # control: still intent
    ])
    def test_classification(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone


class TestGuidanceActionIsTheCompanysOwnOnThisCall:
    """Not a move on this call: another topic, an action dated to a past period, another party's action."""

    @pytest.mark.parametrize("text", [
        # Another topic, not the revenue / earnings outlook.
        "We raised our guidance on capital expenditures.",
        "We lowered our guidance for operating expenses.",
        "We raised our guidance for fiscal 2026 capital spending.",
        "We raised our outlook for total operating costs.",
        "We raised our forecast regarding the tax rate on interest.",
        "Our capex guidance was raised.",
        # Dated to a past period.
        "Last quarter we lowered our full-year guidance.",
        "Last quarter, we lowered our full-year guidance.",
        "In 2020 we withdrew our guidance.",
        "In 2020, we withdrew our guidance.",
        "In 2020 and 2021 we withdrew our guidance.",
        "In Q2 we raised our full-year guidance.",
        "In the second quarter management cut its outlook.",
        "Earlier this year we lowered our guidance.",
        "A year ago we suspended our guidance.",
        "We lowered our full-year guidance last quarter.",
        "We withdrew our guidance in 2020.",
        "We withdrew our guidance back in March.",
        "Guidance was raised last year.",
        "Last quarter our guidance was lowered.",
        "We had raised our guidance.",
        "We previously lowered our guidance.",
        # Recounted, not done on this call.
        "Previously we raised our guidance.",
        "Previously, we raised our full-year guidance.",
        "Since we raised our guidance, demand has softened.",
        "Since we have raised our guidance, demand has softened.",
        "After we raised our guidance, the stock moved.",
        "When we lowered our forecast, the market reacted.",
        "Recall that we cut our guidance.",
        "Recall we cut our guidance.",
        "As a reminder, we lowered our outlook.",
        "As you may recall, we withdrew our guidance.",
        "As we said, we raised our guidance.",
        "Remember that we raised our guidance.",
        # Another party's action.
        "Guidance was raised by peers.",
        "Guidance was lowered by several competitors.",
        "Forecasts were cut by analysts.",
        "We note that peers raised guidance.",
        "Analysts' forecasts were lowered.",
        "Consensus forecasts were raised.",
        "Apple's guidance was raised.",
        # The report's "Management raised forward guidance" stood on these three.
        "Microsoft raised its forecast.",
        "We raised our forecast for credit losses.",
        "We raised our guidance twice last year.",
        # Another party, named: the speaker must be the verb's own subject.
        "Apple raised its guidance.",
        "Microsoft raises its outlook.",
        "Microsoft is raising its full-year guidance.",
        "Microsoft cut its outlook and withdrew its forecast.",
        "Microsoft missed and raised its forecast.",
        "We note that Microsoft raised its forecast.",
        "We think Microsoft raised guidance.",
        "Microsoft said its forecast was raised.",
        "Its forecast was raised.",
        "Microsoft guidance was raised.",
        "The Microsoft forecast was lowered.",
        # Another topic, with no list that could name them all.
        "We lowered our outlook for loan losses.",
        "We raised our forecast of credit losses.",
        "We raised our guidance on credit losses.",
        "We raised our guidance for fiscal 2026 credit losses.",
        "We reaffirmed our guidance for the tax rate.",
        "We maintained our outlook for share repurchases.",
        "We raised our forecast for inflation.",
        "We now expect credit losses above our prior forecast.",
        # Dated, with something between the object and the date.
        "We raised our guidance by 2% last year.",
        "We raised our guidance to $10 billion last quarter.",
        "We lowered our guidance three times in 2020.",
        "We raised our guidance in each of the last two years.",
        "We raised our guidance on two occasions last year.",
        "Last year, as demand fell, we lowered our guidance.",
    ])
    def test_not_this_companys_outlook_move_on_this_call(self, analyzer, text):
        assert analyzer._detect_guidance_tone(text) == "not_stated"

    @pytest.mark.parametrize("text,tone", [
        # A history does not outrank what this call did.
        ("In 2020 we withdrew our guidance. We are raising our full-year guidance.", "raised"),
        ("Last quarter we lowered our guidance, and today we are raising our full-year guidance.",
         "raised"),
        # A stated reason is not another topic.
        ("We raised our guidance on strong demand.", "raised"),
        ("We raised our guidance on lower costs.", "raised"),
        # A "by" with an amount, or the company's own people.
        ("Guidance was raised by 2%.", "raised"),
        ("Our full-year guidance was raised by $1 billion.", "raised"),
        ("Our guidance was cut by about 3%.", "lowered"),
        ("Our guidance was raised by the board.", "raised"),
        ("The company's guidance was raised.", "raised"),
        # A past period that dates something else.
        ("Given last quarter's results, we raised our full-year guidance.", "raised"),
        ("We raised our full-year guidance from the range we gave last quarter.", "raised"),
        ("We raised our full-year guidance from last quarter.", "raised"),
        ("Compared with last year, we raised our full-year guidance.", "raised"),
        # A period named as the guidance's own subject is not a date of the action.
        ("We are raising our 2026 guidance.", "raised"),
        ("For fiscal 2026 we raised our guidance.", "raised"),
        ("We raised our guidance for the full year.", "raised"),
        # Controls: the company acting on its own revenue / earnings outlook.
        ("We raised our forecast.", "raised"),
        ("We raise our outlook.", "raised"),
        ("We lowered our guidance.", "lowered"),
        ("We reaffirm our outlook.", "maintained"),
        ("We reiterated our full-year guidance.", "maintained"),
        ("We withdrew our guidance.", "withdrawn"),
        ("We are withdrawing our forecast.", "withdrawn"),
        ("The company raised its forecast.", "raised"),
        ("Management lowered its full-year guidance.", "lowered"),
        ("Our board of directors raised guidance.", "raised"),
        ("The company said its forecast was raised.", "raised"),
        ("Today guidance was raised.", "raised"),
        ("We once again raised guidance.", "raised"),
        ("We are pleased to raise guidance.", "raised"),
        ("Last quarter we were cautious, but we raised our full-year guidance.", "raised"),
        ("Last quarter we lowered our guidance; we are raising our full-year guidance.", "raised"),
        # A topic that is the outlook.
        ("We raised our forecast for revenue.", "raised"),
        ("We raised our guidance for cloud revenue.", "raised"),
        ("We raised our guidance for adjusted EPS.", "raised"),
        ("We raised our outlook for operating margin.", "raised"),
        ("We raised our guidance for the remainder of the year.", "raised"),
        ("We raised our forecast of $10 billion to $11 billion.", "raised"),
        # A past period that dates something else.
        ("We raised our guidance twice this year.", "raised"),
        ("We raised our guidance on results better than last year.", "raised"),
        ("We raised our guidance after a strong start last quarter.", "raised"),
    ])
    def test_this_calls_own_action_still_counts(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone


class TestTheReportDoesNotCreditAnotherPartysMove:
    """generator.py prints "Management raised forward guidance" on this class
   ."""

    @staticmethod
    def _report(analyzer, line):
        from types import SimpleNamespace

        from finsight.report.generator import ReportGenerator

        t = SimpleNamespace(ticker="X", quarter="Q1", full_text=line, prepared_remarks=line,
                            qa_section="")
        return ReportGenerator().generate("X", sentiment=analyzer.analyze_transcript(t))

    @pytest.mark.parametrize("line", [
        "Microsoft raised its forecast.",
        "We raised our forecast for credit losses.",
        "We raised our guidance twice last year.",
    ])
    def test_no_guidance_claim(self, analyzer, line):
        report = self._report(analyzer, line)
        assert "Management raised forward guidance" not in report.key_highlights
        assert "guidance" not in report.investment_thesis

    def test_the_companys_own_move_still_prints(self, analyzer):
        report = self._report(analyzer, "We raised our full-year guidance.")
        assert "Management raised forward guidance" in report.key_highlights


class TestAPastActionIsNotThisCalls:
    """The controls for "Previously we raised", "Since we raised", "Recall that we cut":
    a word that also opens a recollection does not make this call's action history."""

    @pytest.mark.parametrize("text, tone", [
        ("After strong results we raised our guidance.", "raised"),
        ("After a strong quarter, we raised our guidance.", "raised"),
        ("Since demand is strong, we raised our guidance.", "raised"),
        ("As demand softened, we lowered our guidance.", "lowered"),
        ("Earlier today we raised our guidance.", "raised"),
        ("Previously we expected less, but today we raised our guidance.", "raised"),
        ("We raised our guidance after a strong quarter.", "raised"),
        ("We are raising our guidance since demand is strong.", "raised"),
        ("Recall that revenue grew, and we are raising our guidance.", "raised"),
    ])
    def test_this_calls_action_stays(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone

    def test_the_readme_says_so(self):
        from pathlib import Path

        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        for phrase in ('"previously we raised"', '"since we raised"', '"recall that we cut"'):
            assert phrase in readme, phrase


class TestOurIsWhoseNotWho:
    """ "our" says whose guidance it is, not who acted, and an outlook on the economy is
    not guidance."""

    @pytest.mark.parametrize("text", [
        "S&P lowered our outlook to negative.",
        "Goldman raised our forecast.",
        "Moody's cut our outlook.",
        "We note that Fitch lowered our outlook.",
        "Kelly raised our forecast.",
        "Our outlook was lowered to negative.",
        "Our outlook was lowered to negative by S&P.",
        "We reduced our outlook on inflation.",
        "We lowered our outlook on the economy.",
        "We raised our outlook on U.S. inflation.",
        "We cut our forecast on tariffs.",
        "We lowered our outlook on consumer spending.",
        "Our inflation outlook was lowered.",
    ])
    def test_someone_elses_move_or_another_topic_is_not_stated(self, analyzer, text):
        assert analyzer._detect_guidance_tone(text) == "not_stated"

    @pytest.mark.parametrize("text, tone", [
        # A predicate with no subject of its own takes "our" as the actor.
        ("Raised our full-year guidance.", "raised"),
        ("Also raised our guidance.", "raised"),
        ("Accordingly raised our outlook.", "raised"),
        ("Strong results, and raised our guidance.", "raised"),
        ("We delivered strong results and raised our guidance.", "raised"),
        ("Revenue grew 8%, raising our full-year outlook.", "raised"),
        # The company's own people, and an adverb, are still the company acting.
        ("Management raised our outlook.", "raised"),
        ("Our management team lowered our forecast.", "lowered"),
        ("The board raised our guidance.", "raised"),
        ("Our CFO lowered our outlook.", "lowered"),
        ("We significantly raised our guidance.", "raised"),
        ("We modestly lowered our outlook.", "lowered"),
        # "On" that gives a reason is still the action.
        ("We raised our outlook on strong demand.", "raised"),
        ("We reduced our outlook on weak demand.", "lowered"),
        ("We lowered our outlook on higher inflation.", "lowered"),
        ("We raised our guidance on lower costs.", "raised"),
        ("We cut our outlook to reflect weaker demand.", "lowered"),
    ])
    def test_the_companys_own_move_still_counts(self, analyzer, text, tone):
        assert analyzer._detect_guidance_tone(text) == tone


class TestNegationStopsAtItsSentence:
    """"There were no delays. Strong demand drove results." is Bullish: negation stays in its sentence."""

    @pytest.mark.parametrize("text, net, label", [
        ("There were no delays. Strong demand drove results.", 0.125, "Bullish"),
        ("We had no delays! Strong demand drove results.", 0.125, "Bullish"),
        ("Were there no delays? Strong demand drove results.", 0.125, "Bullish"),
        ("There were no delays; strong demand drove results.", 0.125, "Bullish"),
        ("There were no delays\nStrong demand drove results", 0.125, "Bullish"),
        ("There were no delays.\n\nStrong demand drove results.", 0.125, "Bullish"),
    ])
    def test_a_negator_does_not_reach_the_next_sentence(self, analyzer, text, net, label):
        result = analyzer.analyze_text(text)
        assert result.net_sentiment == pytest.approx(net)
        assert result.sentiment_label == label

    @pytest.mark.parametrize("text, label", [
        ("Demand was not strong.", "Bearish"),
        ("Results were strong. Demand was not strong. Margins were not strong.", "Bearish"),
        ("There were no losses.", "Neutral"),
        ("No U.S. strength emerged.", "Bearish"),      # an initialism ends no sentence
    ])
    def test_inside_a_sentence_it_still_negates(self, analyzer, text, label):
        assert analyzer.analyze_text(text).sentiment_label == label

    def test_key_phrases_read_a_sentence_as_the_score_does(self, analyzer):
        result = analyzer.analyze_text("There were no delays; strong demand drove results. Next.")
        assert result.key_positive_phrases and not result.key_negative_phrases


class TestATrendNeedsTwoResults:
    """No transcripts at all is insufficient data, not "stable": a claim about results that do not exist."""

    def test_none_has_no_trend(self, analyzer):
        assert analyzer.compute_sentiment_trend([]) == {
            "net_sentiment_series": [], "trend_direction": "insufficient_data"}

    def test_one_has_no_trend(self, analyzer):
        from types import SimpleNamespace

        one = SimpleNamespace(overall=SimpleNamespace(net_sentiment=0.1), quarter="Q1")
        assert analyzer.compute_sentiment_trend([one])["trend_direction"] == "insufficient_data"


GUIDANCE_NOT_STATED = [
    "We have not, however, raised our guidance.",
    "We have not, despite strong demand, raised our full-year guidance.",
    "We didn't, however, lower our full-year guidance.",
    "We may, if anything, raise our guidance.",
    "We could, in time, lower our guidance.",
    "On our last earnings call we lowered our full-year guidance.",
    "On our last conference call we raised our full-year guidance.",
    "Two weeks ago we raised our full-year guidance.",
    "A few days ago we lowered our guidance.",
    "Last week we raised our full-year guidance.",
    "Earlier this month we lowered our guidance.",
]


@pytest.mark.parametrize("sentence", GUIDANCE_NOT_STATED)
def test_an_interrupted_negation_or_a_recent_past_action_is_not_this_calls_action(sentence):
    assert SentimentAnalyzer()._detect_guidance_tone(sentence) == "not_stated"


@pytest.mark.parametrize("sentence, tone", [
    ("We have, however, raised our guidance.", "raised"),
    ("However, we raised our full-year guidance.", "raised"),
    ("Demand did not slow, we note, and we raised our full-year guidance.", "raised"),
    ("We raised our full-year guidance.", "raised"),
    ("We lowered our full-year guidance.", "lowered"),
    ("Given last quarter's results, we raised our full-year guidance.", "raised"),
    ("Today we raised our guidance from the range we gave on our last earnings call.", "raised"),
    ("We raised our guidance from the range we gave two weeks ago.", "raised"),
])
def test_the_guidance_controls_still_read(sentence, tone):
    assert SentimentAnalyzer()._detect_guidance_tone(sentence) == tone


@pytest.mark.parametrize("text", [
    "Had we raised our guidance, investors would have cheered.",
    "Investors would have cheered, had we raised our guidance.",
    "Were we to raise our guidance, investors would cheer.",
    "We deny that we raised our guidance.",
    "The company denied that it raised its outlook.",
    "Rumors that we raised our guidance are false.",
    "Analysts believe we raised our guidance.",
    "Some claim that we lowered our outlook.",
    "Reports that we cut our forecast are speculation.",
])
def test_hearsay_denial_and_inverted_conditions_state_no_action(text):
    assert SentimentAnalyzer()._detect_guidance_tone(text) == "not_stated"


@pytest.mark.parametrize("text, tone", [
    ("We raised our guidance.", "raised"),
    ("Today we lowered our full-year outlook.", "lowered"),
    ("Despite the rumors, we raised our guidance.", "raised"),
    ("We believe demand is strong, and we raised our guidance.", "raised"),
    ("As we said, we reaffirm our outlook.", "maintained"),
])
def test_a_real_action_beside_those_words_still_counts(text, tone):
    assert SentimentAnalyzer()._detect_guidance_tone(text) == tone


def test_the_readme_says_it_is_a_heuristic_and_names_a_miss_that_is_one():
    from pathlib import Path

    readme = " ".join((Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8").split())
    assert "It is a heuristic over sentence patterns, not a parser" in readme
    miss = "It is wrong that we raised our guidance."
    assert f'"{miss}" still reads *raised*' in readme
    assert SentimentAnalyzer()._detect_guidance_tone(miss) == "raised"
