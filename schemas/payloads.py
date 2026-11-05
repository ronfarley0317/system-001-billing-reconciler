"""Typed payloads that cross the LLM boundary.

The Scout (LLM) may only *transcribe* contract language into these fields.
Every monetary/percentage field is exposed to the model as a plain string in the
JSON schema and parsed into `Decimal` by Pydantic on our side, so no float ever
touches a financial value.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

_NUMERIC_JUNK = re.compile(r"[,\s$%]|USD", re.IGNORECASE)


def _to_decimal(value: Any) -> Any:
    """Normalise '$12,000.00' / '5.0%' / '250' into Decimal. Rejects floats."""
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # JSON numbers from the model arrive as float: round-trip through repr
        # text so we never inherit binary float error (e.g. 0.1 -> 0.1000000001).
        value = repr(value)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        cleaned = _NUMERIC_JUNK.sub("", value)
        if cleaned == "":
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(f"not a decimal amount: {value!r}") from exc
    raise ValueError(f"unsupported numeric value: {value!r}")


def _money_field(description: str) -> Any:
    return Annotated[
        Decimal,
        BeforeValidator(_to_decimal),
        WithJsonSchema({"type": "string", "description": description}),
    ]


Money = _money_field("Dollar amount exactly as written, digits only, e.g. '1234.00'.")
OptionalMoney = Annotated[
    Optional[Decimal],
    BeforeValidator(_to_decimal),
    WithJsonSchema(
        {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Dollar amount as written, digits only, or null if absent.",
        }
    ),
]
OptionalPercent = Annotated[
    Optional[Decimal],
    BeforeValidator(_to_decimal),
    WithJsonSchema(
        {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Percentage number only, e.g. '3.5' for 3.5%, or null if absent.",
        }
    ),
]


class VerbatimCitation(BaseModel):
    """An exact quote from the contract that supports one extracted field."""

    model_config = ConfigDict(extra="forbid")

    field_name: str = Field(description="Name of the extracted field this quote supports.")
    section: str = Field(description="Contract section number/heading, e.g. '3.2'.")
    quote: str = Field(description="Exact, unmodified sentence copied from the contract.")


class ExtractedContractTerms(BaseModel):
    """Pricing terms transcribed from a contract. Language only - no arithmetic."""

    model_config = ConfigDict(extra="forbid")

    base_rate: Money = Field(description="Standard per-unit monthly fee.")
    promo_rate: OptionalMoney = Field(description="Promotional per-unit monthly fee, if any.")
    promo_expiration_date: Optional[date] = Field(
        description="Last date (inclusive) the promotional rate applies, ISO YYYY-MM-DD."
    )
    annual_escalator_pct: OptionalPercent = Field(
        description="Annual price increase percentage, if any."
    )
    escalator_effective_date: Optional[date] = Field(
        description="Date the first escalation takes effect, ISO YYYY-MM-DD."
    )
    monthly_minimum_floor: OptionalMoney = Field(
        description="Minimum monthly invoice amount regardless of usage, if any."
    )
    verbatim_citations: list[VerbatimCitation] = Field(
        description="One exact quote from the contract for every non-null field above."
    )
