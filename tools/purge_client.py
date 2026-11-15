"""End-of-engagement purge for one client.

    python tools/purge_client.py <client_id>

Deletes that client's findings, audit runs and decision history from the ledger, after
you type the confirmation. A receipt (client id, time, counts, no client data) is kept in
client_purges. Delete the client's local folder (clients/<folder>/ and reports/) yourself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.db_client import purge_client  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    client_id = argv[1]
    expected = f"PURGE {client_id}"
    print(f"This permanently deletes every finding, audit run and decision for '{client_id}'.")
    typed = input(f"Type '{expected}' to continue: ").strip()
    if typed != expected:
        print("Confirmation did not match. Nothing deleted.")
        return 1
    result = purge_client(client_id, typed)
    print(f"Deleted {result['findings_deleted']} findings, {result['runs_deleted']} runs and "
          f"{result['events_deleted']} history entries for {client_id}. Receipt saved in client_purges.")
    print("Now delete the client's local folder and any reports for them.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
