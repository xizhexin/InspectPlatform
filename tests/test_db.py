import os
import sqlite3
import tempfile
import unittest

from app.repo import Repo


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(os.path.join(self.tmp.name, "t.db"), reset=True)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_tables_and_indexes(self):
        tables = {r[0] for r in self.repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(tables, {"user", "device", "plan", "event",
                                  "ticket", "review", "audit_log"})
        idx = {r[0] for r in self.repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'")}
        self.assertIn("idx_ticket_point_status", idx)
        self.assertIn("idx_event_ts", idx)

    def test_unique_dedup_key_is_last_guard(self):
        sql = ("INSERT INTO event(dedup_key, point_id, cls, score, model_ver, route, ts) "
               "VALUES('same-key',1,'rust',0.9,'v1','auto','2026-01-01T00:00:00')")
        self.repo.conn.execute(sql)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.conn.execute(sql)

    def test_stats_empty(self):
        s = self.repo.stats()
        self.assertEqual(s["devices"], 6)
        self.assertEqual(s["events"], 0)


if __name__ == "__main__":
    unittest.main()
