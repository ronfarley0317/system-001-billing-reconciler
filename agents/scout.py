"""Scout agent: contract language -> ExtractedContractTerms.

Boundary: the LLM only transcribes what the contract says. It performs no
arithmetic and its output is never trusted blindly - every citation is checked
deterministically against the source text before the Analyst sees the terms.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openai import OpenAI

from schemas.payloads import ExtractedContractTerms

SYSTEM_PROMPT = """\
You are a contract transcription clerk. Read the contract and copy its pricing
terms into the required JSON schema.

Rules:
- Transcribe only. Never calculate, infer, round, or combine numbers.
- Monetary values: digits and decimal point only (e.g. "250.00"), no symbols.
- Percentages: the number only (e.g. "5.0" for five percent).
- Dates: ISO format YYYY-MM-DD.
- Use null for any term the contract does not state.
- For EVERY non-null field, add a citation whose `quote` is one exact sentence
  copied character-for-character from the contract.
- In `other_pricing_terms`, list any other clause that changes the recurring
  amount billed and does not fit the fields above. Do not interpret it; just name
  it. Do not list payment terms, due dates, late charges, interest or taxes. If
  there are none, return an empty list.
"""


@dataclass(frozen=True)
class CitationCheck:
    field_name: str
    section: str
    quote: str
    verified: bool  # the quote appears in the contract
    value_in_quote: bool  # the extracted value appears inside that same quote

    @property
    def ok(self) -> bool:
        return self.verified and self.value_in_quote


@dataclass(frozen=True)
class ScoutResult:
    terms: ExtractedContractTerms
    citation_checks: list[CitationCheck]
    model: str
    elapsed_s: float

    @property
    def all_citations_verified(self) -> bool:
        return bool(self.citation_checks) and all(c.verified for c in self.citation_checks)

    @property
    def all_values_in_quotes(self) -> bool:
        return bool(self.citation_checks) and all(c.value_in_quote for c in self.citation_checks)

    @property
    def integrity_ok(self) -> bool:
        return self.all_citations_verified and self.all_values_in_quotes


def _normalise(text: str) -> str:
    """Collapse whitespace/markdown emphasis so line-wrapped quotes still match."""
    text = text.replace("**", "").replace("`", "")
    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for token in _NUMBER.findall(text):
        try:
            found.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def _date_forms(d: date) -> set[str]:
    month = f"{d:%B}".lower()
    return {
        f"{month} {d.day}, {d.year}",
        f"{month} {d.day} {d.year}",
        f"{d.day} {month} {d.year}",
        d.isoformat(),
        f"{d.month:02d}/{d.day:02d}/{d.year}",
        f"{d.month}/{d.day}/{d.year}",
    }


_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}
_NUMBER_WORD = "|".join(sorted([*_UNITS, *_TENS, *_SCALES], key=len, reverse=True))
_WORD_RUN = rf"(?:{_NUMBER_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_NUMBER_WORD}))*"
# A number written in words only counts when an explicit unit follows it ("five percent",
# "thirty days", "twelve thousand dollars"). Vague phrases ("a few days") never match.
_WORDS_WITH_UNIT = re.compile(rf"\b({_WORD_RUN})\s+(?:\([^)]*\)\s*)?(percent|per cent|days?|months?|years?|dollars?)\b")
# Contracts often write both forms: "five percent (5.0%)", "thirty (30) days". They must agree.
_WORDS_THEN_DIGITS = re.compile(rf"\b({_WORD_RUN})\s*(?:percent|per cent|dollars?)?\s*\(\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*%?\s*\)")


def _words_to_decimal(phrase: str) -> Decimal | None:
    total, current = 0, 0
    for word in re.split(r"[\s-]+", phrase):
        if word == "and" or not word:
            continue
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in _SCALES:
            total += (current or 1) * _SCALES[word]
            current = 0
        else:
            return None
    return Decimal(total + current)


def _worded_numbers(text: str) -> set[Decimal]:
    found = set()
    for words, _unit in _WORDS_WITH_UNIT.findall(text):
        value = _words_to_decimal(words)
        if value is not None:
            found.add(value)
    return found


def _forms_disagree(text: str) -> bool:
    """True when a quote writes a number both ways and the two forms are different numbers."""
    for words, digits in _WORDS_THEN_DIGITS.findall(text):
        worded = _words_to_decimal(words)
        if worded is not None and worded != Decimal(digits.replace(",", "")):
            return True
    return False


def value_in_quote(value: object, quote: str) -> bool:
    """Is the value the model extracted actually written inside the quote it cited?

    Catches the failure the quote check cannot: a real sentence cited with a misread
    number (e.g. the clause says 5.0% but the field says 50.0). Numbers are compared
    as Decimal, so "$12,000.00", "12,000" and "12000.00" all match 12000.00. A value
    written only in words matches when an explicit unit follows it ("five percent",
    "thirty days"); vague phrases do not. When the quote writes a number both ways,
    "five percent (5.0%)", the two forms must agree or the check fails.
    """
    if value is None:
        return False
    text = _normalise(quote)
    if isinstance(value, date):
        return any(form in text for form in _date_forms(value))
    if isinstance(value, Decimal):
        if _forms_disagree(text):
            return False
        return value in _numbers_in(text) or value in _worded_numbers(text)
    return _normalise(str(value)) in text


def verify_citations(terms: ExtractedContractTerms, contract_text: str) -> list[CitationCheck]:
    haystack = _normalise(contract_text)
    return [
        CitationCheck(
            field_name=c.field_name,
            section=c.section,
            quote=c.quote,
            verified=_normalise(c.quote).rstrip(".") in haystack,
            value_in_quote=value_in_quote(getattr(terms, c.field_name, None), c.quote),
        )
        for c in terms.verbatim_citations
    ]


def _client() -> OpenAI:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    # Ollama ignores the key, but the SDK requires one.
    return OpenAI(base_url=base_url, api_key="ollama")


def extract_terms(contract_path: Path, model: str | None = None, contract_text: str | None = None) -> ScoutResult:
    """`contract_text` is the already-extracted text (PDF, Word...); citations are verified against it."""
    model = model or os.getenv("OLLAMA_MODEL", "qwen3:14b")
    if contract_text is None:
        contract_text = contract_path.read_text(encoding="utf-8")

    started = time.perf_counter()
    completion = _client().chat.completions.parse(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            # `/no_think` disables Qwen3's reasoning trace; we want a clean transcription.
            {"role": "user", "content": f"/no_think\n\nCONTRACT:\n\n{contract_text}"},
        ],
        response_format=ExtractedContractTerms,
    )
    elapsed = time.perf_counter() - started

    message = completion.choices[0].message
    if message.parsed is None:
        raise RuntimeError(f"Scout returned no structured payload: {message.refusal or message.content!r}")

    terms = message.parsed
    return ScoutResult(
        terms=terms,
        citation_checks=verify_citations(terms, contract_text),
        model=model,
        elapsed_s=elapsed,
    )
