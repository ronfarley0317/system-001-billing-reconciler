"""Generate the System #001 demo fixtures.

Creates:
  demo_data/apex_contract_2025.md    - the signed Order Form (source of truth)
  demo_data/erp_invoices_export.csv  - 12 months of ERP invoices with planted leaks

Planted leaks (what the reconciler must find):
  1. The $200 promo rate expired 2025-06-30, but AR kept billing $200 every month.
  2. The 5.0% annual escalator effective 2026-01-01 was never applied.
  3. Seat volume dropped in April 2026, pushing usage below the $12,000 monthly
     minimum, and nobody billed the true-up to the floor.

All money is written with Decimal so the fixture has no float artifacts.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "demo_data"
CONTRACT_PATH = OUT_DIR / "apex_contract_2025.md"
CSV_PATH = OUT_DIR / "erp_invoices_export.csv"

CUSTOMER = "Apex Logistics Group, LLC"
CUSTOMER_ID = "CUST-APEX-0042"
SKU = "MRD-FLEET-SEAT"

# What accounting billed: the promo rate, every month, no matter what.
BILLED_UNIT_PRICE = Decimal("200.00")

# 12 billing months: Jun 2025 (last legit promo month) through May 2026.
# Volume drops from 60 to 40 seats in April 2026.
BILLING_MONTHS: list[tuple[date, int]] = [
    (date(2025, 6, 1), 60),
    (date(2025, 7, 1), 60),
    (date(2025, 8, 1), 60),
    (date(2025, 9, 1), 60),
    (date(2025, 10, 1), 60),
    (date(2025, 11, 1), 60),
    (date(2025, 12, 1), 60),
    (date(2026, 1, 1), 60),
    (date(2026, 2, 1), 60),
    (date(2026, 3, 1), 60),
    (date(2026, 4, 1), 40),  # volume drop
    (date(2026, 5, 1), 40),
]

CONTRACT_MD = f"""\
# MASTER SUBSCRIPTION ORDER FORM

**Order Form No.:** MS-OF-2025-0117
**Provider:** Meridian Fleet Systems, Inc. ("Provider")
**Customer:** {CUSTOMER} ("Customer")
**Customer Account:** {CUSTOMER_ID}
**Order Effective Date:** January 1, 2025
**Initial Term:** Thirty-six (36) months

---

## 1. Background

This Order Form is entered into under, and is governed by, the Master Subscription
Agreement dated December 12, 2024 between Provider and Customer (the "MSA").
Capitalized terms not defined herein have the meanings given in the MSA.

## 2. Subscribed Services

Provider will make the Meridian Fleet Telematics Platform (SKU `{SKU}`) available to
Customer for each vehicle seat activated by Customer (each, an "Active Seat").

## 3. Fees

### 3.1 Standard Subscription Fee

Customer shall pay a subscription fee of $250.00 per Active Seat per month (the
"Base Rate").

### 3.2 Introductory Promotional Rate

As an onboarding incentive, Customer shall pay a reduced rate of $200.00 per Active
Seat per month (the "Promotional Rate") for all billing periods through and including
June 30, 2025. The Promotional Rate shall expire automatically on June 30, 2025, and
the Base Rate shall apply to all billing periods thereafter without further notice.

### 3.3 Annual Price Escalator

Commencing January 1, 2026, and on each anniversary of that date during the Term,
all per-seat Fees shall increase by five percent (5.0%) over the rate in effect
immediately prior to such date.

### 3.4 Monthly Minimum Commitment

Notwithstanding the number of Active Seats in any month, Customer shall pay a
minimum monthly fee of $12,000.00 (the "Monthly Minimum"). If the per-seat Fees
calculated for a month are less than the Monthly Minimum, Provider shall invoice the
Monthly Minimum for that month.

## 4. Invoicing and Payment

4.1 Fees are invoiced monthly in advance on the first day of each billing period.

4.2 Payment is due net thirty (30) days from the invoice date.

4.3 Overdue amounts accrue a late charge of 1.5% per month or the maximum permitted
by law, whichever is less.

## 5. Taxes

Fees are exclusive of all sales, use, and similar taxes, which Customer shall pay.

## 6. Signatures

| Provider                           | Customer                          |
|------------------------------------|-----------------------------------|
| Meridian Fleet Systems, Inc.       | {CUSTOMER}         |
| By: /s/ Dana R. Whitfield          | By: /s/ Marcus L. Chen            |
| Title: VP, Revenue Operations      | Title: Chief Financial Officer    |
| Date: December 18, 2024            | Date: December 20, 2024           |
"""

CSV_HEADER = [
    "invoice_id",
    "invoice_date",
    "customer_id",
    "customer_name",
    "sku",
    "description",
    "quantity",
    "unit_price",
    "amount_billed",
    "currency",
    "status",
]


def build_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for i, (inv_date, seats) in enumerate(BILLING_MONTHS, start=1):
        amount = (BILLED_UNIT_PRICE * seats).quantize(Decimal("0.01"))
        rows.append(
            {
                "invoice_id": f"INV-{inv_date:%Y%m}-{1000 + i}",
                "invoice_date": inv_date.isoformat(),
                "customer_id": CUSTOMER_ID,
                "customer_name": CUSTOMER,
                "sku": SKU,
                "description": f"Fleet Telematics - {inv_date:%b %Y} - {seats} seats",
                "quantity": str(seats),
                "unit_price": f"{BILLED_UNIT_PRICE:.2f}",
                "amount_billed": f"{amount:.2f}",
                "currency": "USD",
                "status": "PAID",
            }
        )
    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(CONTRACT_MD, encoding="utf-8")

    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(build_rows())

    print(f"wrote {CONTRACT_PATH.relative_to(ROOT)}")
    print(f"wrote {CSV_PATH.relative_to(ROOT)} ({len(BILLING_MONTHS)} rows)")


if __name__ == "__main__":
    main()
