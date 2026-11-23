import os
import tempfile
import unittest

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.repo import Repo


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(os.path.join(self.tmp.name, "t.db"), reset=True)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_tables_and_indexes(self):
        insp = inspect(self.repo.engine)
        self.assertEqual(set(insp.get_table_names()),
                         {"user", "device", "plan", "event",
                          "ticket", "review", "audit_log"})
        idx = {i["name"] for t in ("ticket", "event", "device", "review", "audit_log")
               for i in insp.get_indexes(t)}
        self.assertIn("idx_ticket_point_status", idx)
        self.assertIn("idx_event_ts", idx)
        self.assertIn("idx_device_area", idx)

    def test_unique_dedup_key_is_last_guard(self):
        sql = text(
            "INSERT INTO event(dedup_key, point_id, cls, score, model_ver, route, ts) "
            "VALUES('same-key', 1, 'rust', 0.9, 'v1', 'auto', '2026-01-01T00:00:00')")
        with self.repo.transaction() as s:
            s.execute(sql)
        with self.assertRaises(IntegrityError):
            with self.repo.transaction() as s:
                s.execute(sql)

    def test_stats_empty(self):
        s = self.repo.stats()
        self.assertEqual(s["devices"], 6)
        self.assertEqual(s["events"], 0)


if __name__ == "__main__":
    unittest.main()
