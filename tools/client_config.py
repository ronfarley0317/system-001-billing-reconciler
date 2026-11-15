"""A client's permanent id, stored with their files: clients/<folder>/client.json.

The ledger keys every finding by client id, so it must never change and never collide.
It is created once, on the first run for that folder, from the folder name plus a random
suffix, and read back on every run after. Renaming the company, or a second client with
the same name, cannot move findings between clients.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

CONFIG_NAME = "client.json"
_VALID = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "client"


def load_or_create_client_id(folder: Path) -> tuple[str, bool]:
    """Return (client_id, created_now)."""
    path = folder / CONFIG_NAME
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        client_id = str(data.get("client_id", ""))
        if not _VALID.match(client_id):
            raise ValueError(f"{path} has an invalid client_id {client_id!r}. "
                             "Fix it by hand (lowercase letters, digits, dashes) or delete the file to make a new one.")
        return client_id, False
    client_id = f"{_slug(folder.name)}-{secrets.token_hex(3)}"
    path.write_text(json.dumps({"client_id": client_id,
                                "note": "Permanent ledger id for this client. Do not change it."}, indent=2) + "\n",
                    encoding="utf-8")
    return client_id, True
