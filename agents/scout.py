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


def value_in_quote(value: object, quote: str) -> bool:
    """Is the value the model extracted actually written inside the quote it cited?

    Catches the failure the quote check cannot: a real sentence cited with a misread
    number (e.g. the clause says 5.0% but the field says 50.0). Numbers are compared
    as Decimal, so "$12,000.00", "12,000" and "12000.00" all match 12000.00. Values
    written only in words ("five percent") do not match; that is flagged for review
    on purpose rather than guessed.
    """
    if value is None:
        return False
    text = _normalise(quote)
    if isinstance(value, date):
        return any(form in text for form in _date_forms(value))
    if isinstance(value, Decimal):
        return value in _numbers_in(text)
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
