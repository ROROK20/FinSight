"""
Financial sentiment analysis of earnings-call text. It scores any text it is given,
but the API's only source is earnings-call transcripts: nothing here reads SEC filings.

Two modes:
1. Lexicon (default) — word counts against FinSight's own small, curated
   finance word lists (below). Fast, no GPU, no model download.
2. FinBERT (optional) — the ProsusAI/finbert classifier, reported BESIDE the
   word-list score, never folded into it (see :meth:`SentimentAnalyzer.analyze_text`).

The word lists are **inspired by** the Loughran-McDonald (LM) Master Dictionary
(https://sraf.nd.edu/loughranmcdonald-master-dictionary/), the standard
reference for financial tone, but they are **not** the LM dictionary: 161
hand-picked entries (63 positive, 63 negative, 35 uncertainty), chosen
so that a word counts only if it carries tone on its own: direction words ("increased",
"higher") and earnings-call vocabulary ("earnings", "guidance") count for nothing. The full
LM dictionary is not bundled: its terms of use have not been reviewed for redistribution.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class WordList:
    """A curated word list: exact words, plus explicit stems written ``"stem*"``.

    Only an entry ending in ``*`` matches by prefix (``"deteriorat*"`` matches
    "deteriorated" and "deterioration"); every other entry matches the whole
    word only, so "confident" never matches "confidential".
    """

    def __init__(self, entries: set[str]) -> None:
        self.words = frozenset(e for e in entries if not e.endswith("*"))
        self.stems = tuple(sorted(e[:-1] for e in entries if e.endswith("*")))

    def __contains__(self, word: str) -> bool:
        return word in self.words or any(word.startswith(s) for s in self.stems)

    def hits(self, words: list[str]) -> int:
        """How many of ``words`` (lower-case tokens) are on the list."""
        return sum(1 for w in words if w in self)


# Tone words only. Deliberately absent: words that say which way a number
# moved without saying whether that is good (increase/decrease, higher/lower,
# grew/declined, expand/reduce, growth, significant), and words that are just
# the vocabulary of an earnings call (earnings, guidance, record, value).
POSITIVE = WordList({
    "achieve", "achieved", "achievement", "achievements", "advantage",
    "advantages", "beneficial", "benefit", "benefited", "benefits", "best",
    "better", "breakthrough", "breakthroughs", "confident", "effective",
    "efficiencies", "efficiency", "efficient", "excellent", "exceptional",
    "favorable", "gain", "gained", "gains", "great", "improve", "improved",
    "improvement", "improvements", "improves", "improving", "innovative",
    "opportunities", "opportunity", "optimistic", "outperform", "outperformed",
    "outperforming", "pleased", "profitable", "profitability", "progress",
    "rebound", "rebounded", "resilient", "robust", "strength", "strengthen",
    "strengthened", "strengths", "strong", "stronger", "strongest", "succeed",
    "succeeded", "success", "successful", "successfully", "superior", "win",
    "winning", "wins",
})

NEGATIVE = WordList({
    "adverse", "adversely", "challenge", "challenged", "challenges",
    "challenging", "concern", "concerned", "concerns", "deficit", "deficits",
    "deteriorat*", "difficult", "difficulties", "difficulty", "disappoint*",
    "disruption", "disruptions", "downturn", "downturns", "fail", "failed",
    "failing", "failure", "failures", "headwind", "headwinds", "impair*",
    "inadequate", "layoff", "layoffs", "loss", "losses", "miss", "missed",
    "negative", "negatively", "obstacle", "obstacles", "penalties", "penalty",
    "poor", "poorly", "pressure", "pressured", "pressures", "problem",
    "problems", "shortage", "shortages", "slowdown", "unfavorable", "weak",
    "weaken", "weakened", "weaker", "weakness", "weaknesses", "worries",
    "worry", "worse", "worsened", "worsening",
})

# Hedging and uncertainty. Not tone, and not part of the net score.
UNCERTAINTY = WordList({
    "approximately", "appear", "appears", "believe", "believes", "could",
    "depend", "depending", "depends", "doubt", "fluctuate", "fluctuation",
    "fluctuations", "likely", "may", "maybe", "might", "pending", "perhaps",
    "possible", "possibly", "risk", "risks", "risky", "seem", "seems",
    "uncertain", "uncertainties", "uncertainty", "unclear", "unknown",
    "unpredictable", "variable", "volatile", "volatility",
})


# Guidance tone: only an explicit action on guidance counts (see
# SentimentAnalyzer._detect_guidance_tone).
GUIDANCE_TONES = ("raised", "lowered", "mixed", "maintained", "initiated",
                  "withdrawn", "not_stated")
_GUIDANCE_NOUN = re.compile(r"\b(?:guidance|outlook|forecasts?)\b")

# The action verb must GOVERN the guidance noun: it is followed directly by the
# object phrase — determiners and modifiers only ("raised our full-year revenue
# guidance"), no other word or clause in between, so "We reduced costs and raised our
# full-year guidance." is a raise, not mixed, and "Revenue increased 8% and we reaffirm our
# outlook." a reaffirmation.
_MODIFIER = (
    r"(?:our|its|the|this|that|full[- ]year|annual|quarterly|fiscal|"
    r"(?:fiscal\s+)?(?:year\s+)?20\d\d|fy\s?\d{2,4}|q[1-4]|(?:first|second|third|fourth|"
    r"next)[- ](?:quarter|half)|revenue|sales|earnings|eps|margin|operating|profit|"
    r"ebitda|financial|initial|current|long[- ]term)"
)
# Revenue / earnings guidance only: "capex", "capital", "spending", "expense",
# "tax" are not modifiers here, so "raised capex guidance" is not a raise.
_OBJECT = rf"(?:{_MODIFIER}\s+){{0,5}}(?:guidance|outlook|forecasts?)\b"
# "guidance for the full year is unchanged": a for-phrase between noun and verb.
_FOR_PHRASE = (r"(?:\s+for\s+(?:the\s+)?(?:full[- ]year|year|fiscal(?:\s+year)?(?:\s+20\d\d)?|"
               r"20\d\d|(?:first|second|third|fourth|next)\s+(?:quarter|half)|quarter))?")

# Only a DECLARATIVE, REALISED action counts. A clause that negates,
# hedges, plans or asks states no action: "We are not raising our guidance",
# "We may raise our guidance later this year", "It is too early to raise
# guidance", "We plan to withdraw guidance", "We'd raise our guidance if
# demand holds".
_NOT_REALISED = re.compile(
    r"\b(?:not|no|never|nor|without|won't|wont|don't|dont|doesn't|didn't|isn't|aren't|"
    r"wasn't|haven't|hasn't|cannot|can't|unlikely|whether|may|might|could|would|should|"
    r"shall|will|must|can|maybe|perhaps|possibly|potentially|likely|consider(?:s|ed|ing)?|"
    r"plan(?:s|ned|ning)?|hope(?:s|d)?|intend(?:s|ed)?|aim(?:s|ed)?|expect(?:s|ed)?\s+to|"
    r"going\s+to|early|asked|ask|question|"
    # Hearsay and denial report a claim, not an action: "We deny that we raised our
    # guidance", "Rumors that we raised…", "Analysts believe we raised…".
    r"den(?:y|ies|ied|ying)|rumou?rs?|speculat\w*|alleg\w*|claim\w*|believ\w*|thinks?|"
    r"suggest\w*|reports?\s+that)\b|'d\b|n't\b"
)
# A comma-set aside does not end a negation or a modal: "We have not, however, raised our
# guidance" and "We may, if anything, raise our guidance" are not raises.
_INTERRUPTED_HEDGE = re.compile(
    r"\b(not|never|\w+n't|may|might|could|would|should|shall|will|must|can)\s*,[^,;:]{1,40},\s*")
# A condition anywhere in the sentence makes the action hypothetical.
# So does one by inversion: "Had we raised our guidance, investors would have cheered."
_CONDITION = re.compile(
    r"\b(?:if|unless|provided|assuming|should|(?:had|were)\s+(?:we|i|the company|management))\b")
# A question (by mark or by its opening words) states no action.
_QUESTION_START = re.compile(
    r"^\s*(?:analyst:\s*)?(?:why|how|what|when|where|which|who|will|would|could|can|"
    r"do|does|did|are|is|was|were|have|has|should)\b")
# Where a clause starts, looking back from the verb.
_CLAUSE_BREAK = re.compile(r"[,;:()]|\b(?:and|but|while|whereas|although|though|because)\b")
# The company must be the one acting: a first-person / company subject earlier
# in the sentence, or the company's own guidance as the object ("our
# guidance"), so "Analysts raised their forecasts" and "The stock fell on lower guidance"
# are not actions. ("their" is not a modifier at all: "Competitors have lowered their
# guidance" is theirs.)
_SUBJECT = re.compile(r"\b(?:we|i|management|the company|our (?:company|board|team))\b")
# Only "our" says whose guidance it is on its own. "Its" needs the company as its
# antecedent, which "Microsoft raised its forecast." does not give it.
_OWN_OBJECT = re.compile(r"^our\b")
# But "our" says only whose guidance it is, not who acted: "S&P lowered our outlook to
# negative" and "Goldman raised our forecast" are not this company's actions. It
# settles the actor only for a predicate with no subject of its own: "Raised our guidance."
_NO_SUBJECT_OF_ITS_OWN = re.compile(
    r"^\s*(?:(?:today|now|also|so|thus|therefore|hence|then|again|further|just|recently|already|"
    r"accordingly|consequently|additionally|finally|separately|subsequently)\s+)*$")
# The company must be the verb's own subject: only an auxiliary, an adverb or an
# announcing "pleased to" may sit between them. "We note that Microsoft raised its
# forecast" has a "we", but it is not the one acting.
_COMPANY_ACTS = re.compile(
    r"\b(?:we|i|(?:our\s+)?(?:management|leadership)(?:\s+team)?|the company|"
    r"the board(?: of directors)?|our (?:company|board(?: of directors)?|team|ceo|cfo))"
    r"(?:'ve|'re|'m)?\s+"
    r"(?:(?:have|has|are|is|am|been|now|also|just|today|again|once|recently|already|"
    r"therefore|thus|then|further|hereby|formally|so|both|all|\w+ly|"
    r"this\s+(?:morning|quarter))\s+)*"
    r"(?:(?:pleased|happy|delighted|glad|proud)\s+to\s+)?$")
# What may lead a bare passive ("today guidance was raised"). Anything else is
# someone's name: "Microsoft guidance was raised".
_BARE_PASSIVE_LEAD = re.compile(
    r"(?:^|\b(?:today|now|so|thus|therefore|accordingly|result|the|company's|management's|"
    r"board's|team's))\s*$")
# "our forecast for capital expenditures", "our guidance on capital
# expenditures", "our guidance for operating expenses": another topic, not the
# revenue / earnings outlook. Only determiners and modifiers may sit before the
# topic, so "guidance on lower costs" stays a raise for a stated reason.
_NON_OUTLOOK_TOPIC = (r"(?:capital|capex|expenditures?|expenses?|opex|costs?|tax(?:es)?|"
                      r"spending|investments?|interest|depreciation|share\s+count|"
                      r"headcount|buybacks?|dividends?|credit|loss(?:es)?|provisions?|"
                      r"charge-?offs?|write-?(?:offs?|downs?)|restructuring|inventor(?:y|ies)|"
                      r"repurchases?|churn|"
                      # The economy, not the company: "We reduced our outlook on inflation".
                      # "On higher inflation" gives a reason, and "higher" is no modifier
                      # below, so that one still counts.
                      r"inflation|deflation|rates?|tariffs?|economy|gdp|recession|unemployment|"
                      r"macro(?:economic)?(?:\s+\w+)?|oil|commodit(?:y|ies)|currenc(?:y|ies)|fx|"
                      r"housing)")
# What a "guidance for / of / about …" may be about: a period, or the revenue and
# earnings outlook. Anything else is another topic: no list of topics could name
# them all ("We raised our forecast for credit losses."). "On" usually gives a reason ("on strong demand"), so it is tested
# only against _NOT_GUIDANCE_FOR.
_ABOUT_THE_OUTLOOK = (
    r"(?:(?:the|our|its|both|total|adjusted|non-?gaap|gaap|diluted|this|that)\s+)*(?:"
    r"[$\d]|full[- ]year|year\b|fiscal\b|fy\b|fy\s?\d|calendar\b|q[1-4]\b|h[12]\b|quarter\b|"
    r"(?:first|second|third|fourth|next|current|coming|upcoming)\b|remainder\b|rest\b|balance\b|"
    r"annual\b|growth\b|margins?\b|profit(?:s|ability)?\b|ebitda\b|income\b|results?\b|"
    r"demand\b|top[- ]line\b|bottom[- ]line\b|operating\s+(?:income|margins?|profit)\b|"
    r"(?:[\w-]+\s+){0,3}(?:revenues?|sales|earnings|eps)\b)"
)
_ANOTHER_TOPIC = re.compile(
    rf"\s+(?:for|of|regarding|around|concerning|about)\s+(?!{_ABOUT_THE_OUTLOOK})")
_NOT_GUIDANCE_FOR = re.compile(
    r"\s+(?:for|on|of|regarding|around|concerning|about)\s+"
    r"(?:(?:the|our|its|total|fiscal|annual|full[- ]year|next|year'?s?|operating|non-?gaap|"
    rf"u\.?s\.?|global|domestic|near[- ]term|overall|broader|general|consumer|"
    rf"20\d\d)\s+){{0,4}}{_NON_OUTLOOK_TOPIC}\b")
# A credit rating's outlook, not guidance: "Our outlook was lowered to negative."
_RATING_OUTLOOK = re.compile(r"\s+to\s+(?:negative|positive|stable)\b")
# The same topic before the noun of a passive ("our capex guidance was raised").
_NON_OUTLOOK_BEFORE = re.compile(rf"\b{_NON_OUTLOOK_TOPIC}(?:\s+expenditures?)?\s*$")

# Someone else's action. "We note that peers raised guidance" has a company
# subject earlier in the sentence, so the subject test alone does not catch it;
# "Guidance was raised by peers" names its agent after the participle; "Analysts'
# forecasts were lowered" names the owner before the noun. A "by" that gives an
# amount, or names the company's own people, is still the company's.
_OTHER_ACTOR = re.compile(
    r"\b(?:peers?|competitors?|rivals?|analysts?|customers?|suppliers?|others?|they|he|she|"
    r"banks?|brokers?|companies|firms?)\s+(?:(?:have|has|also|all|recently|just)\s+)*$")
_OTHER_AGENT = re.compile(
    r"\s+by\s+(?![$\d]|about\b|approximately\b|roughly\b|around\b|nearly\b|almost\b|more\b|"
    r"over\b|up\b|at\b|another\b|a\s+(?:further|few|couple|total)\b|the\s+company\b|"
    r"the\s+board\b|the\s+management\b|management\b|us\b|our\b)")
_OTHER_OWNER = re.compile(
    r"(?:\b(?:peers?|competitors?|rivals?|analysts?|consensus|street|industry|sector|their|his|"
    r"her)|\b[a-z]+s'|\b(?!company\b|management\b|board\b|team\b)[a-z]+'s)\s*$")

# An action dated to a past period is history, not what this call did: "Last
# quarter we lowered our full-year guidance", "In 2020 we withdrew our guidance",
# "We lowered our guidance last year". The date must sit right at the action:
# "Given last quarter's results, we raised…" and "raised our guidance from the
# range we gave last quarter" are this call's raise.
_MONTH = (r"(?:january|february|march|april|may|june|july|august|september|october|"
          r"november|december)")
_YEARS = r"(?:19|20)\d\d(?:\s*(?:and|or|,)\s*(?:19|20)\d\d)*"
_PAST_PERIOD = (
    r"(?:\b(?:last|previous|prior|past)\s+(?:(?:two|three|four|five|several|few|\d+)\s+)?"
    r"(?:fiscal\s+|calendar\s+|earnings\s+|conference\s+)?"
    r"(?:years?|quarters?|months?|weeks?|fall|spring|summer|winter|calls?|time)\b(?!')"
    r"|\b(?:years?|quarters?|months?|weeks?|days?)\s+ago\b"
    rf"|\b(?:back\s+)?(?:in|during)\s+(?:fiscal\s+|fy\s?|calendar\s+)?(?:year\s+)?{_YEARS}\b"
    r"|\b(?:back\s+)?(?:in|during)\s+(?:the\s+)?(?:q[1-4]|(?:first|second|third|fourth)\s+quarter|"
    r"(?:first|second)\s+half)\b"
    rf"|\b(?:back\s+)?in\s+{_MONTH}(?:\s+(?:19|20)\d\d)?\b"
    r"|\bearlier\s+(?:this|in\s+the)\s+(?:year|quarter|month|week)\b"
    r"|\bat\s+(?:that|the)\s+time\b)"
)
_DATED_BEFORE = re.compile(
    rf"(?:{_PAST_PERIOD}\s*,?\s*"
    r"(?:(?:we|i|management|the company|our (?:company|board|team))\s+)?"
    r"(?:also\s+|then\s+|already\s+)?"
    r"|\b(?:had|previously|originally)\s+)$")
_DATED_AFTER = re.compile(rf"{_FOR_PHRASE}\s*,?\s+{_PAST_PERIOD}")
# "Compared with last year, we raised…": the period is the comparison, not the date.
_COMPARED = re.compile(r"\b(?:compared\s+(?:with|to)|versus|vs\.?|than|from|since|relative\s+to)\s*$")
# After the object, a past period dates the action wherever it falls in the clause
# ("raised our guidance twice last year", "by 2% last year"), unless one of these
# says it dates something else ("from the range we gave last quarter").
_NOT_THE_ACTIONS_DATE = re.compile(
    r"\b(?:from|than|compared|versus|vs\.?|since|relative|against|over|above|below|after|"
    r"following|given|due|despite)\b")
# "Last year, as demand fell, we lowered our guidance": a sentence opening on a
# past period dates a past-tense action later in it, unless it turns to the present.
# So does one that opens by recalling it: "Previously, we raised", "Recall that we cut",
# "As a reminder, we lowered".
_OPENS_ON_THE_PAST = re.compile(
    rf"^\s*(?:{_PAST_PERIOD}|(?:previously|formerly|originally|earlier|recall|remember)\b"
    r"|as\s+a\s+reminder\b|as\s+(?:we|i|you)\s+(?:may\s+)?(?:recall|remember|know|said|"
    r"mentioned|noted|discussed)\b)")
# "Since we raised our guidance, demand softened": the action is the clause's backdrop.
# The word must lead the speaker directly: "After strong results we raised" is this call's.
_RECOUNTED = re.compile(
    r"\b(?:since|after|when|once|before|until|recall(?:ed)?(?:\s+that)?|remember(?:\s+that)?)\s+"
    r"(?:we|i|management|the company|our (?:company|board|team))\b")
_TURNS_TO_THE_PRESENT = re.compile(r"\b(?:but|however|yet|today|now|this\s+(?:quarter|call|year))\b")


def _dated_to_a_past_period(before: str, rest: str, verb: str = "") -> bool:
    dated = _DATED_BEFORE.search(before)
    if _DATED_AFTER.match(rest) or (dated and not _COMPARED.search(before[:dated.start()])):
        return True
    if (verb.endswith("ed") or verb in ("cut", "withdrew")) and _OPENS_ON_THE_PAST.match(before) \
            and not _TURNS_TO_THE_PRESENT.search(before):
        return True
    clause = _CLAUSE_BREAK.split(rest, maxsplit=1)[0]
    period = re.search(_PAST_PERIOD, clause)
    return bool(period and not _NOT_THE_ACTIONS_DATE.search(clause[:period.start()]))
_QUARTERLY = re.compile(r"\b(?:q[1-4]|quarterly|quarter)\b|"
                        r"(?:first|second|third|fourth|next)[- ]quarter|\s+for\s+the\s+\w+\s+quarter")
# "We are pleased to raise our guidance" announces the action, not an intent.
_PERFORMATIVE_TO = re.compile(r"\b(?:pleased|happy|delighted|glad|proud)\s+to\s*$")
# Before an "-ing" verb: present progressive only ("we are raising").
_PROGRESSIVE = re.compile(r"(?:\b(?:am|is|are|now)|'re)\s*$")


# A sentence ends at . ! ? or ; before whitespace, but not in an initialism: "U.S. inflation".
_SENTENCE_END = re.compile(r"(?<=[.!?;])(?<!\.[a-zA-Z]\.)\s+")
# How far a negator reaches: its sentence, and no further than its line, since a
# transcript page's text comes out one block per line, a heading with no full stop included.
_TONE_BOUNDARY = re.compile(rf"{_SENTENCE_END.pattern}|\n+")


def _states_no_action(sentence: str) -> bool:
    return (sentence.rstrip().endswith("?") or bool(_QUESTION_START.match(sentence))
            or bool(_CONDITION.search(sentence)))


class _Action:
    """A declarative, realised action on guidance: ``<verb> <object>``
    (active: past tense, present tense or present progressive — never an
    infinitive "to raise") or ``<object> is/was <participle>`` (passive), in a
    clause with no negation, modal, plan or hedge."""

    def __init__(self, verbs: str, passive: str, annual_only: bool = False) -> None:
        # annual_only: issuing a QUARTER's guidance is routine, not "initiated".
        self.annual_only = annual_only
        self.active = re.compile(rf"\b(?P<verb>{verbs})\s+{_OBJECT}")
        self.passive = re.compile(
            rf"\b{_OBJECT}{_FOR_PHRASE}\s+(?:is|was|has been|have been|are|were|remains?)\s+"
            rf"(?:{passive})\b")

    def search(self, sentence: str) -> bool:
        if _states_no_action(sentence):
            return False
        sentence = _INTERRUPTED_HEDGE.sub(r"\1 ", sentence)
        for pattern in (self.active, self.passive):
            for m in pattern.finditer(sentence):
                before = sentence[:m.start()]
                breaks = list(_CLAUSE_BREAK.finditer(before))
                clause = before[breaks[-1].end():] if breaks else before
                if _NOT_REALISED.search(clause + " " + m.group(0)):
                    continue
                verb = m.groupdict().get("verb") or ""
                # Active voice needs the company as the actor: "our" guidance, or
                # the company as this verb's own subject. A predicate with no
                # subject of its own ("…and raised guidance", "…, reaffirming
                # guidance") takes the sentence's.
                if verb and not (
                    (_OWN_OBJECT.match(m.group(0)[len(verb):].strip())
                     and _NO_SUBJECT_OF_ITS_OWN.match(clause))
                    or _COMPANY_ACTS.search(clause)
                    or (not clause.strip() and _SUBJECT.search(before))
                ):
                    continue          # someone else's action, or an adjective
                rest = sentence[m.end():]
                if (_NOT_GUIDANCE_FOR.match(rest) or _ANOTHER_TOPIC.match(rest)
                        or _RATING_OUTLOOK.match(rest)):
                    continue          # "our forecast for credit losses"
                if verb:
                    if _OTHER_ACTOR.search(before):
                        continue      # "we note that peers raised guidance"
                elif (_OTHER_AGENT.match(rest) or _OTHER_OWNER.search(before)
                      or _NON_OUTLOOK_BEFORE.search(before)):
                    continue          # "was raised by peers", "our capex guidance was raised"
                elif m.group(0).startswith("its"):
                    if not _SUBJECT.search(before):
                        continue      # "its forecast was raised": whose?
                elif (not _OWN_OBJECT.match(m.group(0))
                      and not _BARE_PASSIVE_LEAD.search(clause)):
                    continue          # "microsoft guidance was raised"
                if _dated_to_a_past_period(before, rest, verb) or _RECOUNTED.search(clause):
                    continue          # "last quarter we lowered": history, not this call
                if self.annual_only and (_QUARTERLY.search(m.group(0)) or _QUARTERLY.match(rest)):
                    continue          # "second-quarter guidance": routine, not initiated
                if re.search(r"\bto\s*$", clause) and not _PERFORMATIVE_TO.search(clause):
                    continue          # an infinitive: intent, not action
                # A participle opening its own clause after a comma attaches to
                # a declarative main clause ("…, reaffirming our guidance").
                participle = not clause.strip() and before.rstrip().endswith(",")
                if verb.endswith("ing") and not (_PROGRESSIVE.search(clause) or participle):
                    continue          # a gerund, not "we are raising"
                return True
        return False


_GUIDANCE_ACTIONS: dict[str, _Action] = {
    "raised": _Action(r"rais(?:e|es|ed|ing)|lift(?:s|ed|ing)?|boost(?:s|ed|ing)?|"
                      r"upgrad(?:e|es|ed|ing)|increas(?:e|es|ed|ing)",
                      r"raised|lifted|boosted|upgraded|increased"),
    "lowered": _Action(r"lower(?:s|ed|ing)?|reduc(?:e|es|ed|ing)|cut(?:s|ting)?|"
                       r"trim(?:s|med|ming)?|downgrad(?:e|es|ed|ing)",
                       r"lowered|reduced|cut|trimmed|downgraded"),
    "maintained": _Action(r"reaffirm(?:s|ed|ing)?|reiterat(?:e|es|ed|ing)|"
                          r"maintain(?:s|ed|ing)?|confirm(?:s|ed|ing)?",
                          r"reaffirmed|reiterated|maintained|confirmed|unchanged"),
    "initiated": _Action(r"initiat(?:e|es|ed|ing)|introduc(?:e|es|ed|ing)|issu(?:e|es|ed|ing)",
                         r"initiated|introduced|issued", annual_only=True),
    "withdrawn": _Action(r"withdr(?:aw|aws|awn|awing|ew)|suspend(?:s|ed|ing)?|pull(?:s|ed|ing)?",
                         r"withdrawn|suspended|pulled"),
}


_NEGATORS = frozenset({"not", "no", "never", "without", "hardly", "neither", "nor",
                       "cannot", "barely"})


def _tokens(text: str) -> list[str]:
    """Lower-case word tokens, with "n't" spelled out as "not"."""
    text = text.lower().replace("n't", " not").replace("n\u2019t", " not")
    return re.findall(r"\b[a-z]+\b", text)


def _tone_counts(text: str) -> tuple[int, int]:
    """(positive, negative) tone words, with negation handled.

    A tone word with a negator among the three words before it, in its own
    sentence, is negated: a negated positive counts as negative ("demand was
    not strong"), and a negated negative counts as neither ("no losses" is not
    praise). A negator does not cross sentences: in "There were no delays. Strong demand
    drove results." the second sentence is positive.
    Phrase-level meaning is still out of reach of a word list: "Losses narrowed
    significantly" counts "losses" as negative and scores Bearish although the
    news is good.
    """
    pos = neg = 0
    for sentence in _TONE_BOUNDARY.split(text):
        words = _tokens(sentence)
        for i, w in enumerate(words):
            negated = any(t in _NEGATORS for t in words[max(0, i - 3):i])
            if w in POSITIVE:
                if negated:
                    neg += 1
                else:
                    pos += 1
            elif w in NEGATIVE and not negated:
                neg += 1
    return pos, neg


def _lexicon_hits(words: list[str], lexicon: WordList) -> int:
    """Words in ``lexicon`` — shared by the score and the key-phrase picker so
    both read a sentence the same way."""
    return lexicon.hits(words)


@dataclass
class SentimentResult:
    """Sentiment analysis results for a piece of text."""

    text_source: str  # 'prepared_remarks', 'qa_section', or the caller's own label
    positive_score: float  # fraction of words that are positive
    negative_score: float  # fraction of words that are negative
    uncertainty_score: float
    net_sentiment: float  # positive - negative
    polarity: str  # 'positive', 'negative', 'neutral'
    word_count: int
    key_positive_phrases: list[str] = field(default_factory=list)
    key_negative_phrases: list[str] = field(default_factory=list)
    finbert_score: dict[str, float] | None = None  # {'positive': 0.8, 'negative': 0.1, 'neutral': 0.1}
    finbert_net: float | None = None
    """FinBERT p(positive) − p(negative), averaged over the scored chunks, in [−1, 1]."""
    blended_tone: float | None = None
    """0.6 × ``finbert_net`` + 0.4 × the word-list tone (positive − negative
    words) / (positive + negative words) — both on the same [−1, 1] scale. The
    weights are a fixed heuristic, never tuned or validated for accuracy."""
    finbert_status: str | None = None
    """None when FinBERT was not requested; otherwise "ok", "partial: …",
    "failed: …" or "unavailable: …" — a FinBERT failure is never a silent 0."""
    finbert_chunks: int = 0
    """How many text chunks FinBERT scored."""

    @property
    def sentiment_label(self) -> str:
        if self.net_sentiment > 0.02:
            return "Bullish"
        elif self.net_sentiment < -0.02:
            return "Bearish"
        return "Neutral"

    def __repr__(self) -> str:
        return (
            f"SentimentResult({self.text_source} | {self.polarity} | "
            f"net={self.net_sentiment:+.3f} | "
            f"words={self.word_count})"
        )


@dataclass
class TranscriptSentiment:
    """Full sentiment analysis of an earnings transcript."""

    ticker: str
    quarter: str
    prepared_remarks: SentimentResult
    qa_section: SentimentResult
    overall: SentimentResult
    sentiment_drift: float | None  # Q&A minus prepared remarks; None without a Q&A section
    guidance_tone: str  # one of GUIDANCE_TONES
    risk_mentions: int

    @property
    def is_positive(self) -> bool:
        return self.overall.net_sentiment > 0.01

    def __repr__(self) -> str:
        return (
            f"TranscriptSentiment({self.ticker} {self.quarter} | "
            f"{self.overall.sentiment_label} | "
            f"guidance={self.guidance_tone})"
        )


class SentimentAnalyzer:
    """
    Financial sentiment analyzer: FinSight's curated finance word lists
    (inspired by, not the same as, Loughran-McDonald) and optional FinBERT.

    Parameters
    ----------
    use_finbert : bool
        If True, loads FinBERT from HuggingFace for deep learning sentiment.
        Requires: transformers, torch. Default False (uses lexicon).
    finbert_model : str
        HuggingFace model ID for FinBERT (default: 'ProsusAI/finbert').
    max_chunk_size : int
        Characters per chunk sent to FinBERT (default 2000). Each chunk is also
        truncated to the model's 512-token limit by the pipeline itself.
    max_chunks : int
        At most this many chunks are scored (default 25 = the first 50,000
        characters, the transcript fetcher's own cap); the result records how
        many were.

    Examples
    --------
    >>> analyzer = SentimentAnalyzer()
    >>> result = analyzer.analyze_text("Revenue grew 15% driven by strong AI demand.", "transcript")
    >>> print(result.polarity, result.net_sentiment)

    >>> # Analyze full transcript
    >>> transcript_result = analyzer.analyze_transcript(transcript)
    >>> print(transcript_result.overall.sentiment_label)
    """

    def __init__(
        self,
        use_finbert: bool = False,
        finbert_model: str = "ProsusAI/finbert",
        max_chunk_size: int = 2000,
        max_chunks: int = 25,
    ) -> None:
        self.use_finbert = use_finbert
        self.finbert_model = finbert_model
        self.max_chunk_size = max_chunk_size
        self.max_chunks = max_chunks
        self._finbert_pipeline = None
        # Why FinBERT is off although it was requested (None = not requested
        # or loaded fine). Carried on every result so it cannot go unnoticed.
        self._finbert_unavailable: str | None = None

        if use_finbert:
            self._load_finbert()

    def _load_finbert(self) -> None:
        try:
            from transformers import pipeline  # type: ignore
            self._finbert_pipeline = pipeline(
                "text-classification",
                model=self.finbert_model,
                top_k=None,
            )
            logger.info("FinBERT loaded successfully.")
        except ImportError:
            logger.warning("transformers not installed. FinBERT unavailable; word lists only.")
            self._finbert_unavailable = "unavailable: transformers is not installed"
            self.use_finbert = False
        except Exception as e:
            logger.warning(f"FinBERT load failed: {e}. FinBERT unavailable; word lists only.")
            self._finbert_unavailable = f"unavailable: model load failed ({type(e).__name__})"
            self.use_finbert = False

    def analyze_text(
        self,
        text: str,
        source_label: str = "text",
    ) -> SentimentResult:
        """
        Analyze sentiment of a text block.

        ``net_sentiment`` is always the word-list score. With FinBERT on, the
        result ALSO carries ``finbert_score`` / ``finbert_net``,
        ``blended_tone`` (both scores on one [−1, 1] scale, 0.6 / 0.4 — an
        unvalidated heuristic) and ``finbert_status``. It never overwrites ``net_sentiment``,
        and a failed FinBERT chunk is counted in the status, not scored as 0.

        Parameters
        ----------
        text : str
            Text to analyze.
        source_label : str
            Label for the source (used in result metadata).

        Returns
        -------
        SentimentResult
        """
        result = self._lexicon_sentiment(text, source_label)

        if self._finbert_unavailable:
            result.finbert_status = self._finbert_unavailable
        elif self.use_finbert and self._finbert_pipeline is not None:
            scores, status, n_chunks = self._finbert_sentiment(text)
            result.finbert_status = status
            result.finbert_chunks = n_chunks
            if scores is not None:
                # Reported beside net_sentiment, never folded into it: the
                # word-list net is a fraction of ALL words (typically within
                # ±0.05) while FinBERT's is a probability difference in
                # [−1, 1], so a direct blend would give the word lists no weight.
                # The blend below puts both on [−1, 1].
                result.finbert_score = scores
                result.finbert_net = scores.get("positive", 0.0) - scores.get("negative", 0.0)
                hits = result.positive_score + result.negative_score
                word_tone = (result.positive_score - result.negative_score) / hits if hits else 0.0
                result.blended_tone = 0.6 * result.finbert_net + 0.4 * word_tone

        return result

    def analyze_transcript(self, transcript: Any) -> "TranscriptSentiment":
        """
        Analyze a full earnings transcript.

        Parameters
        ----------
        transcript : EarningsTranscript
            Transcript with prepared_remarks and qa_section.

        Returns
        -------
        TranscriptSentiment
        """
        remarks_result = self.analyze_text(
            transcript.prepared_remarks or transcript.full_text,
            "prepared_remarks",
        )
        qa_result = self.analyze_text(
            transcript.qa_section or "",
            "qa_section",
        )
        full_result = self.analyze_text(
            transcript.full_text,
            "full_transcript",
        )

        # Sentiment drift: how much more negative the Q&A is vs prepared.
        # None without a Q&A section: an empty Q&A scores 0, and reporting
        # 0 − remarks as "drift" would be a made-up number.
        drift = (qa_result.net_sentiment - remarks_result.net_sentiment
                 if qa_result.word_count else None)

        # Guidance tone detection
        guidance_tone = self._detect_guidance_tone(transcript.full_text)

        # Risk mention count
        # Plurals count too ("Risks, headwinds, challenges").
        risk_mentions = len(re.findall(
            r"\b(?:risks?|uncertaint(?:y|ies)|challenges?|headwinds?|pressures?)\b",
            transcript.full_text,
            re.IGNORECASE,
        ))

        return TranscriptSentiment(
            ticker=transcript.ticker,
            quarter=transcript.quarter,
            prepared_remarks=remarks_result,
            qa_section=qa_result,
            overall=full_result,
            sentiment_drift=drift,
            guidance_tone=guidance_tone,
            risk_mentions=risk_mentions,
        )

    def batch_analyze(self, texts: list[str], labels: list[str] | None = None) -> list[SentimentResult]:
        """Analyze multiple texts. Returns list of SentimentResult."""
        labels = labels or [f"text_{i}" for i in range(len(texts))]
        return [self.analyze_text(t, l) for t, l in zip(texts, labels)]

    def compute_sentiment_trend(
        self,
        results: list["TranscriptSentiment"],
    ) -> dict[str, Any]:
        """
        Compute sentiment trend over multiple quarters.

        Parameters
        ----------
        results : list[TranscriptSentiment]
            List ordered oldest to newest.

        Returns
        -------
        dict with: net_sentiment_series, trend_slope, trend_direction.
        ``trend_direction`` is "insufficient_data" with fewer than two results.
        """
        sentiments = [r.overall.net_sentiment for r in results]
        quarters = [r.quarter for r in results]

        if len(sentiments) < 2:
            return {"net_sentiment_series": sentiments, "trend_direction": "insufficient_data"}

        # Linear trend slope
        x = np.arange(len(sentiments))
        slope = float(np.polyfit(x, sentiments, 1)[0])

        return {
            "quarters": quarters,
            "net_sentiment_series": sentiments,
            "trend_slope": slope,
            "trend_direction": "improving" if slope > 0.005 else
                               "deteriorating" if slope < -0.005 else "stable",
        }

    # ------------------------------------------------------------------
    # private: lexicon
    # ------------------------------------------------------------------

    def _lexicon_sentiment(self, text: str, source_label: str) -> SentimentResult:
        """Word-list sentiment: (positive − negative words) / all words."""
        words = _tokens(text)
        if not words:
            return SentimentResult(
                text_source=source_label,
                positive_score=0, negative_score=0,
                uncertainty_score=0, net_sentiment=0,
                polarity="neutral", word_count=0,
            )

        n = len(words)
        pos_count, neg_count = _tone_counts(text)
        unc_count = _lexicon_hits(words, UNCERTAINTY)

        pos_score = pos_count / n
        neg_score = neg_count / n
        unc_score = unc_count / n
        net = pos_score - neg_score

        polarity = "positive" if net > 0.01 else "negative" if net < -0.01 else "neutral"

        # Extract key phrases (sentences with most sentiment words)
        pos_phrases, neg_phrases = self._extract_key_phrases(text)

        return SentimentResult(
            text_source=source_label,
            positive_score=pos_score,
            negative_score=neg_score,
            uncertainty_score=unc_score,
            net_sentiment=net,
            polarity=polarity,
            word_count=n,
            key_positive_phrases=pos_phrases[:3],
            key_negative_phrases=neg_phrases[:3],
        )

    def _finbert_sentiment(
        self, text: str
    ) -> tuple[dict[str, float] | None, str, int]:
        """Mean FinBERT class probabilities over the text's chunks.

        Returns ``(scores, status, chunks_scored)``. ``scores`` is None when no
        chunk could be scored. Each chunk is passed with ``truncation=True`` so
        an over-long chunk is cut to the model limit instead of raising, and
        every failure is logged at WARNING and counted in ``status``.
        """
        chunks = [text[i:i + self.max_chunk_size]
                  for i in range(0, len(text), self.max_chunk_size)]
        skipped = max(0, len(chunks) - self.max_chunks)
        chunks = chunks[:self.max_chunks]
        scores: dict[str, list[float]] = {"positive": [], "negative": [], "neutral": []}
        failures: list[str] = []
        scored = 0

        for i, chunk in enumerate(chunks):
            try:
                result = self._finbert_pipeline(chunk, truncation=True, max_length=512)
                inner = result[0] if result and isinstance(result[0], list) else result
                got = False
                for item in inner or []:
                    label = str(item["label"]).lower()
                    if label in scores:
                        scores[label].append(float(item["score"]))
                        got = True
                if not got:
                    raise ValueError(f"no recognised labels in {result!r:.80}")
                scored += 1
            except Exception as e:
                logger.warning(f"FinBERT chunk {i + 1}/{len(chunks)} failed: {e}")
                failures.append(type(e).__name__)

        note = f"; {skipped} chunk(s) past the first {self.max_chunks} not scored" if skipped else ""
        if not scored:
            reason = failures[0] if failures else "no text"
            return None, f"failed: no chunk could be scored ({reason}){note}", 0
        status = (f"partial: {len(failures)} of {len(chunks)} chunks failed"
                  if failures else "ok") + note
        return {k: float(np.mean(v)) if v else 0.0 for k, v in scores.items()}, status, scored

    def _extract_key_phrases(
        self, text: str
    ) -> tuple[list[str], list[str]]:
        """Up to five net-positive and five net-negative sentences, strongest first.

        A sentence is a key positive only if it has at least one positive
        lexicon word and more positive than negative words, and a key negative
        (a "concern") only in the mirror case — counted with the same lexicon
        matching the score uses. Fewer qualifying sentences means a shorter
        list, possibly empty, so a neutral sentence is never reported as a key concern.
        """
        sentences = re.split(r"[.!?]\s+", text)[:200]  # cap for speed

        positive: list[tuple[float, int, str]] = []
        negative: list[tuple[float, int, str]] = []
        for i, sent in enumerate(sentences):
            words = _tokens(sent)
            phrase = sent[:200].strip()
            if not words or not phrase:
                continue
            pos, neg = _tone_counts(sent)
            net = (pos - neg) / len(words)
            if pos > neg:
                positive.append((net, i, phrase))
            elif neg > pos:
                negative.append((net, i, phrase))

        # Strongest first; ties keep transcript order.
        positive.sort(key=lambda x: (-x[0], x[1]))
        negative.sort(key=lambda x: (x[0], x[1]))
        return [s for *_, s in positive[:5]], [s for *_, s in negative[:5]]

    @staticmethod
    def _implied_change(sentence: str) -> str | None:
        """ "We now expect revenue above our prior guidance" — a realised
        revision stated against the prior guidance."""
        if _states_no_action(sentence):
            return None
        m = re.search(r"\bwe\s+now\s+(?:expect|see|anticipate)\b(?P<rest>[^.;]*?)\b"
                      r"(?P<dir>above|higher than|ahead of|below|lower than|short of)\s+"
                      r"(?:our\s+|the\s+)?(?:prior|previous|earlier|original)\s+"
                      r"(?:guidance|outlook|forecasts?)\b", sentence)
        if not m or _NOT_REALISED.search(m.group("rest")):
            return None
        if not re.search(r"\b(?:revenues?|sales|earnings|eps|profits?|income|margins?|results?)\b",
                         m.group("rest")):
            return None           # "we now expect credit losses above our prior forecast"
        return "raised" if m.group("dir") in ("above", "higher than", "ahead of") else "lowered"

    def _detect_guidance_tone(self, text: str) -> str:
        """Classify the call's EXPLICIT guidance action.

        Returns one of :data:`GUIDANCE_TONES`. Only a stated action on
        guidance / outlook / forecast counts — "we are raising our full-year
        guidance", "we reaffirm our outlook", "guidance is withdrawn". Results
        measured against guidance ("revenue came in above our guidance") are
        not a guidance change, and a bare mention of guidance is
        ``"not_stated"``. The verb must take the guidance phrase as its direct
        object ("raised our full-year guidance"), and a negated or hypothetical
        clause ("we are not raising", "no plans to withdraw", "whether we would
        raise") states no action — nor does a modal, conditional, plan or
        question ("we may raise", "if demand holds", "too early to raise",
        "are you going to raise guidance?"): the classifier prefers
        ``not_stated`` to a guess. So does an action on another topic ("our
        guidance on capital expenditures", "our forecast for credit losses"), by
        another party ("raised by peers", "Microsoft raised its forecast", "S&P lowered
        our outlook": a company named in the third person cannot be told from another
        one, so the speaker must be the subject) or dated to a past period ("last quarter we
        lowered", "we raised our guidance twice last year"). It is a heuristic
        over sentence patterns, not a parser, and has misses: "It is wrong that we raised our
        guidance." reads raised.
        """
        found: set[str] = set()
        for sentence in _SENTENCE_END.split(text.lower()):
            if not _GUIDANCE_NOUN.search(sentence):
                continue
            found |= {a for a, pattern in _GUIDANCE_ACTIONS.items() if pattern.search(sentence)}
            implied = self._implied_change(sentence)
            if implied:
                found.add(implied)
        if "withdrawn" in found:
            return "withdrawn"
        if {"raised", "lowered"} <= found:
            return "mixed"
        for action in ("raised", "lowered", "maintained", "initiated"):
            if action in found:
                return action
        return "not_stated"
