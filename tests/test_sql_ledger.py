"""Runs the real SQL migrations against a throwaway local Postgres.

Skipped automatically when Postgres binaries are not installed. No Supabase project,
credentials or network are used: the cluster lives in a temp folder and is removed.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SQL = Path(__file__).resolve().parent.parent / "sql"


def _pg_bin() -> str | None:
    found = shutil.which("initdb")
    if found:
        return str(Path(found).parent)
    candidates = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))
    return str(Path(candidates[-1]).parent) if candidates else None


PG_BIN = _pg_bin()


def _as_db_user(cmd: list[str]) -> list[str]:
    """initdb refuses to run as root; drop to the postgres user when we are root."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:  # Linux/macOS only; Windows has no root
        import pwd
        try:
            pwd.getpwnam("postgres")
        except KeyError:
            return cmd
        return ["runuser", "-u", "postgres", "--"] + cmd
    return cmd


@unittest.skipUnless(PG_BIN, "Postgres binaries not installed")
class LedgerSQL(unittest.TestCase):
    PORT = "54329"

    @classmethod
    def create_base_table(cls):
        cls.psql(SQL / "001_create_detected_leaks.sql")
        cls.psql(SQL / "002_add_leak_description.sql")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sys001-pg-")
        os.chmod(cls.tmp, 0o777)
        data, sock = os.path.join(cls.tmp, "data"), cls.tmp
        subprocess.run(_as_db_user([f"{PG_BIN}/initdb", "-D", data, "-A", "trust", "-U", "postgres"]),
                       check=True, capture_output=True)
        subprocess.run(_as_db_user([f"{PG_BIN}/pg_ctl", "-D", data, "-l", os.path.join(cls.tmp, "log"), "-w",
                                    "-o", f"-k {sock} -c listen_addresses='' -p {cls.PORT}", "start"]),
                       check=True, capture_output=True)
        cls.data, cls.sock = data, sock
        cls.create_base_table()
        # Two legacy rows written by the old code, before 003 exists.
        cls.sql("insert into detected_leaks (client_name, invoice_number, calculated_arrears, status) values "
                "('Apex Logistics Group, LLC', 'INV-1', 3000.00, 'APPROVED'), "
                "('Apex Logistics Group, LLC', 'INV-2', 3000.00, 'PENDING_APPROVAL')")
        cls.psql(SQL / "003_audit_runs_and_scoped_approvals.sql")
        cls.psql(SQL / "003_audit_runs_and_scoped_approvals.sql")  # running it twice must be safe
        cls.sql("insert into authorized_approvers values (42, 'Ron', true)")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(_as_db_user([f"{PG_BIN}/pg_ctl", "-D", cls.data, "-m", "immediate", "stop"]),
                       capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _psql(cls, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(_as_db_user([f"{PG_BIN}/psql", "-h", cls.sock, "-p", cls.PORT, "-U", "postgres",
                                           "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-qtA"] + args),
                              capture_output=True, text=True)

    @classmethod
    def psql(cls, path: Path) -> None:
        out = cls._psql(["-f", str(path)])
        if out.returncode:
            raise AssertionError(out.stderr)

    @classmethod
    def sql(cls, statement: str) -> str:
        out = cls._psql(["-c", statement])
        if out.returncode:
            raise AssertionError(out.stderr)
        return out.stdout.strip()

    def sql_fails(self, statement: str) -> str:
        out = self._psql(["-c", statement])
        self.assertNotEqual(out.returncode, 0, f"expected failure: {statement}")
        return out.stderr

    def new_run(self, client="apex", contract="MS-OF-2025-0117") -> str:
        return self.sql(f"select start_audit_run('{client}', '{contract}')")

    def record(self, run, key, amount="3000.00", status="PENDING_APPROVAL", cls_="UNDERBILLED") -> str:
        return self.sql(f"select record_finding('{run}', '{key}', 'Apex', 'INV', 'INV', '2025-07-01', "
                        f"'{cls_}', '{status}', {amount}, 'cite', 'why', '')")

    def decide(self, run, decision="APPROVE", user=42) -> dict:
        return json.loads(self.sql(f"select decide_audit_run('{run}', '{decision}', {user})"))

    # ------------------------------------------------------------------ #
    def test_legacy_rows_kept_and_backfilled(self):
        rows = self.sql("select invoice_number, status, client_id, contract_id, finding_key from detected_leaks "
                        "where contract_id = 'legacy' order by invoice_number")
        self.assertEqual(rows.splitlines(), [
            "INV-1|APPROVED|apex-logistics-group-llc|legacy|legacy:INV-1",
            "INV-2|PENDING_APPROVAL|apex-logistics-group-llc|legacy|legacy:INV-2",
        ])

    def test_same_invoice_number_for_two_clients(self):
        a, b = self.new_run("client-a", "C1"), self.new_run("client-b", "C1")
        self.record(a, "period:2025-07")
        self.record(b, "period:2025-07")
        self.assertEqual(self.sql("select count(*) from detected_leaks where finding_key = 'period:2025-07' "
                                  "and contract_id = 'C1'"), "2")

    def test_full_approval_flow_and_replay(self):
        run = self.new_run("flow", "C-FLOW")
        self.record(run, "period:2025-07", "3000.00")
        self.record(run, "period:2025-08", "3000.00")
        self.record(run, "period:2025-09", "0.00", status="REVIEW_REQUIRED", cls_="MISSING_INVOICE")
        opened = json.loads(self.sql(f"select open_audit_run('{run}')"))
        self.assertEqual((opened["approvable_total"], opened["approvable_count"], opened["review_count"]),
                         ("6000.00", 2, 1))

        self.assertFalse(self.decide(run, user=999)["ok"])  # stranger
        result = self.decide(run)
        self.assertTrue(result["ok"])
        self.assertEqual((result["count"], result["total"]), (2, "6000.00"))
        self.assertEqual(self.sql(f"select status from detected_leaks where audit_run_id = '{run}' "
                                  "and finding_key = 'period:2025-09'"), "REVIEW_REQUIRED")
        again = self.decide(run)  # second tap on the same card
        self.assertFalse(again["ok"])
        self.assertIn("no longer open", again["reason"])
        events = self.sql(f"select count(*) from finding_events e join detected_leaks d on d.id = e.finding_id "
                          f"where d.audit_run_id = '{run}' and e.to_status = 'APPROVED' and e.actor like 'Ron%'")
        self.assertEqual(events, "2")

    def test_rerun_keeps_decisions_and_supersedes_old_card(self):
        first = self.new_run("rerun", "C-RERUN")
        self.record(first, "period:2025-07")
        self.sql(f"select open_audit_run('{first}')")
        self.assertTrue(self.decide(first)["ok"])

        second = self.new_run("rerun", "C-RERUN")
        self.assertEqual(self.record(second, "period:2025-07", "9999.00"), "KEPT_APPROVED")
        self.record(second, "period:2025-08")
        self.sql(f"select open_audit_run('{second}')")
        self.assertEqual(self.sql("select calculated_arrears from detected_leaks where client_id = 'rerun' "
                                  "and finding_key = 'period:2025-07'"), "3000.00")

        third = self.new_run("rerun", "C-RERUN")
        self.record(third, "period:2025-08")
        self.sql(f"select open_audit_run('{third}')")
        stale = self.decide(second)  # the older card
        self.assertFalse(stale["ok"])
        self.assertIn("superseded", stale["reason"])
        self.assertTrue(self.decide(third)["ok"])

    def test_changed_amount_after_card_is_rejected(self):
        run = self.new_run("changed", "C-CHG")
        self.record(run, "period:2025-07")
        self.sql(f"select open_audit_run('{run}')")
        self.sql(f"update detected_leaks set calculated_arrears = 5000.00 where audit_run_id = '{run}'")
        result = self.decide(run)
        self.assertFalse(result["ok"])
        self.assertIn("changed after this card", result["reason"])
        self.assertEqual(self.sql(f"select status from detected_leaks where audit_run_id = '{run}'"),
                         "PENDING_APPROVAL")

    def test_decided_rows_are_immutable(self):
        err = self.sql_fails("update detected_leaks set calculated_arrears = 1 where finding_key = 'legacy:INV-1'")
        self.assertIn("immutable", err)
        self.assertIn("cannot be deleted", self.sql_fails("delete from detected_leaks where finding_key = 'legacy:INV-1'"))

    def test_events_are_append_only(self):
        self.assertIn("append-only", self.sql_fails("delete from finding_events"))

    def test_overbilling_cannot_be_approvable(self):
        run = self.new_run("over", "C-OVER")
        err = self.sql_fails(f"select record_finding('{run}', 'period:2025-07', 'A', 'I', 'I', '2025-07-01', "
                             "'OVERBILLED', 'PENDING_APPROVAL', -3000.00, '', '', '')")
        self.assertIn("approvable_is_underbilling", err)

    def test_review_items_cannot_be_approved_directly(self):
        run = self.new_run("review", "C-REV")
        self.record(run, "period:2025-07", "100.00", status="REVIEW_REQUIRED")
        err = self.sql_fails(f"update detected_leaks set status = 'APPROVED' where audit_run_id = '{run}'")
        self.assertIn("only PENDING_APPROVAL can be approved", err)



class LedgerSQLUuidTable(LedgerSQL):
    """A detected_leaks table made by hand in Supabase: uuid ids and a differently named unique index."""
    PORT = "54330"

    @classmethod
    def create_base_table(cls):
        cls.sql("""
            create table public.detected_leaks (
              id uuid primary key default gen_random_uuid(),
              client_name text not null,
              invoice_number text not null,
              calculated_arrears numeric(10, 2) not null,
              contract_citation text,
              leak_description text,
              status text not null default 'PENDING_APPROVAL',
              created_at timestamptz not null default now());
            create unique index detected_leaks_invoice_uniq on public.detected_leaks (invoice_number);""")

    def test_old_invoice_number_uniqueness_is_gone(self):
        a, b = self.new_run("dup-a", "C"), self.new_run("dup-b", "C")
        self.record(a, "period:2025-07")
        self.record(b, "period:2025-07")  # same invoice_number 'INV' as row above; must not collide


if __name__ == "__main__":
    unittest.main()
