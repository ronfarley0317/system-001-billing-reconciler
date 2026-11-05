import os
from decimal import Decimal

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

CENT = Decimal("0.01")


def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise ValueError("Supabase credentials missing from .env")

    return create_client(url, key)


def log_leak(client_name: str, invoice_number: str, arrears: Decimal, citation: str, description: str):
    """Upsert one detected leak into `detected_leaks`, keyed on `invoice_number`.

    `status` is omitted on purpose: the database defaults it to PENDING_APPROVAL
    on insert, and an upsert only updates the columns we send, so a rerun never
    resets a status a human has already changed.
    """
    supabase = get_supabase_client()
    data = {
        "client_name": client_name,
        "invoice_number": invoice_number,
        # Decimal isn't JSON-serializable, and float would lose precision.
        # Send the exact cent value as a string; Postgres NUMERIC(10, 2) parses it losslessly.
        "calculated_arrears": str(arrears.quantize(CENT)),
        "contract_citation": citation,
        "leak_description": description,
    }
    return (
        supabase.table("detected_leaks")
        .upsert(data, on_conflict="invoice_number")
        .execute()
    )
