"""Output guardrails — applied to the LLM's text before it reaches the user.

The important one is the hallucination check. The pipeline treats the LLM as a
narrator, not a calculator: every figure it is allowed to state must already
exist in a Pandas result (a tool's `grounding` list, the cached profile, or the
composed `data_summary`). Any number that matches nothing gets its sentence
removed and the turn flagged. This is what makes the numbers in the response
trustworthy regardless of which free model served it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

FLAG_HALLUCINATION = "hallucination_corrected"
FLAG_TOXICITY = "toxic_content_filtered"
FLAG_LOW_CONFIDENCE = "low_confidence"
FLAG_INSUFFICIENT_DATA = "insufficient_data"
FLAG_EMPTY_RESPONSE = "empty_llm_response"
FLAG_SCAFFOLDING = "model_scaffolding_stripped"

# Markup a model emits when it tries to call a tool as *text* instead of as a
# structured tool_calls field. Some models fall back to this when confused, and
# the result is that the pipeline treats the call as prose and shows it to the
# user:
#
#     <tool_call>plot_team_overview <arg_key>user_id</arg_key> …
#
# It is never a legitimate answer. Detected rather than repaired: a textual call
# was never executed, so whatever ran was a *different* tool, and salvaging the
# text would narrate an action that did not happen.
SCAFFOLDING_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"<\s*/?\s*tool_call\s*>",
        r"<\s*/?\s*function_call\s*>",
        r"<\s*/?\s*(?:arg_key|arg_value|parameter|tool_response)\s*>",
        r"<\|\s*(?:python_tag|tool_call|function)\s*\|>",
        r"<\s*/?\s*(?:im_start|im_end)\s*\|?>",
        # A bare call object as the whole answer.
        r"^\s*\{\s*[\"']?(?:name|tool|function|tool_name)[\"']?\s*:",
        r"^\s*(?:functions\.)?plot_[a-z_]+\s*\(",
    )
)

# Currency amounts, percentages and bare numbers, in that priority order.
_NUMBER_RE = re.compile(
    r"(?P<currency>[$€£]\s?-?\d[\d,]*(?:\.\d+)?)"
    r"|(?P<percent>-?\d+(?:\.\d+)?\s?%)"
    r"|(?P<bare>(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w%]))"
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

HEDGING_MARKERS = (
    "i'm not sure", "i am not sure", "not entirely sure", "it's unclear", "it is unclear",
    "i don't have enough", "i do not have enough", "hard to say", "can't be certain",
    "cannot be certain", "i think it might", "possibly around", "roughly guessing",
    "i'm guessing", "i am guessing", "no way to tell", "unable to determine",
    "i don't have access", "i do not have access",
)

# Deliberately short and unambiguous: this is a financial tool, so the realistic
# risk is an insult slipping into a "you overspent" message, not open-ended abuse.
TOXICITY_TERMS = frozenset(
    """
    fuck fucking shit bitch bastard asshole dumbass idiot moron stupid pathetic
    loser worthless disgusting scum retard damn crap
    """.split()
)

INSUFFICIENT_DATA_MESSAGE = (
    "I don't have enough data to answer that confidently. {detail}"
)
FALLBACK_AFTER_STRIP = (
    "I checked your transactions but couldn't produce a reliable narrative summary for that. "
    "Here is what the data actually shows: {facts}"
)


@dataclass
class OutputGuardrailResult:
    response: str
    flags: list[str] = field(default_factory=list)
    removed_sentences: list[str] = field(default_factory=list)
    ungrounded_values: list[float] = field(default_factory=list)


def extract_numbers(text: str) -> list[tuple[str, float, bool]]:
    """Returns `[(matched_text, value, is_percent)]`."""
    found: list[tuple[str, float, bool]] = []
    for match in _NUMBER_RE.finditer(text or ""):
        token = match.group(0)
        is_percent = match.lastgroup == "percent"
        cleaned = re.sub(r"[^\d.\-]", "", token)
        if cleaned in {"", "-", ".", "-."}:
            continue
        try:
            found.append((token, float(cleaned), is_percent))
        except ValueError:
            continue
    return found


class OutputGuardrails:
    def __init__(
        self,
        rel_tolerance: float = 0.02,
        abs_tolerance: float = 1.0,
        toxicity_terms: Iterable[str] = TOXICITY_TERMS,
    ):
        self.rel_tolerance = rel_tolerance
        self.abs_tolerance = abs_tolerance
        self.toxicity_terms = frozenset(t.lower() for t in toxicity_terms)

    # -- 1. hallucination -----------------------------------------------------

    def is_grounded(self, value: float, grounding: set[float]) -> bool:
        # Calendar-ish and structural integers the model legitimately uses for
        # phrasing ("over the last 6 months", "in 2025", "3 categories").
        if float(value).is_integer():
            as_int = int(value)
            if 0 <= as_int <= 31 or 1900 <= as_int <= 2100:
                return True
        for truth in grounding:
            if abs(value - truth) <= self.abs_tolerance:
                return True
            if truth and abs(value - truth) / abs(truth) <= self.rel_tolerance:
                return True
        return False

    def check_hallucination(self, text: str, grounding: Iterable[float]) -> tuple[str, list[str], list[float]]:
        """Strip sentences containing figures that no computation produced."""
        truth = {abs(float(g)) for g in grounding if g is not None}
        # Rounded forms count as grounded: "$1,850" for 1850.37 is fair narration.
        truth |= {round(v) for v in truth} | {round(v, 1) for v in truth}
        if not text:
            return text, [], []

        kept: list[str] = []
        removed: list[str] = []
        ungrounded: list[float] = []

        for sentence in _SENTENCE_RE.split(text):
            bad = [
                value
                for _, value, _ in extract_numbers(sentence)
                if not self.is_grounded(abs(value), truth)
            ]
            if bad:
                removed.append(sentence.strip())
                ungrounded.extend(bad)
            else:
                kept.append(sentence)

        if not removed:
            return text, [], []
        return " ".join(s.strip() for s in kept).strip(), [FLAG_HALLUCINATION], ungrounded

    # -- 2. toxicity ----------------------------------------------------------

    @staticmethod
    def detect_scaffolding(text: str) -> bool:
        """True if the model emitted tool-call markup instead of an answer."""
        return any(p.search(text or "") for p in SCAFFOLDING_PATTERNS)

    def check_toxicity(self, text: str) -> tuple[str, list[str]]:
        tokens = set(re.findall(r"[a-z']+", (text or "").lower()))
        if tokens & self.toxicity_terms:
            return (
                "I generated a response that didn't meet our content standards, so I've withheld it. "
                "Please ask your question again.",
                [FLAG_TOXICITY],
            )
        return text, []

    # -- 3. confidence gating -------------------------------------------------

    @staticmethod
    def detect_hedging(text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker in lowered for marker in HEDGING_MARKERS)

    # -- entry point ----------------------------------------------------------

    def check(
        self,
        response: str,
        grounding: Iterable[float] = (),
        data_available: bool = True,
        empty_data_detail: str = "",
        deterministic_facts: Optional[str] = None,
    ) -> OutputGuardrailResult:
        flags: list[str] = []
        text = (response or "").strip()

        if not data_available:
            return OutputGuardrailResult(
                INSUFFICIENT_DATA_MESSAGE.format(detail=empty_data_detail).strip(),
                [FLAG_INSUFFICIENT_DATA],
            )

        if not text:
            return OutputGuardrailResult(
                deterministic_facts or "I wasn't able to produce an answer for that. Please try rephrasing.",
                [FLAG_EMPTY_RESPONSE],
            )

        # Checked before anything else that inspects the prose: scaffolding is
        # not a wrong answer to be corrected, it is not an answer at all.
        if self.detect_scaffolding(text):
            return OutputGuardrailResult(
                deterministic_facts
                or "I couldn't produce a reliable answer for that. Please try rephrasing.",
                [FLAG_SCAFFOLDING],
            )

        text, tox_flags = self.check_toxicity(text)
        if tox_flags:
            return OutputGuardrailResult(text, tox_flags)

        text, halluc_flags, ungrounded = self.check_hallucination(text, grounding)
        flags.extend(halluc_flags)

        if self.detect_hedging(text):
            flags.append(FLAG_LOW_CONFIDENCE)

        if not text.strip():
            # Everything was stripped: fall back to plain computed facts rather
            # than returning nothing.
            text = (
                FALLBACK_AFTER_STRIP.format(facts=deterministic_facts)
                if deterministic_facts
                else "I couldn't produce a reliable answer for that. Please try rephrasing your question."
            )

        return OutputGuardrailResult(text.strip(), sorted(set(flags)), [], ungrounded)
