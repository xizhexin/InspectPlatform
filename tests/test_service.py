import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ai.detector import StubDetector
from ai.service import InspectionService
from app.repo import Repo

FRAME = {"defects": [
    {"cls": "indicator_abnormal", "score": 0.95},
    {"cls": "lane_occupied", "score": 0.55},
    {"cls": "noise", "score": 0.10},
]}
LOW = {"defects": [{"cls": "lane_occupied", "score": 0.55}]}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(os.path.join(self.tmp.name, "t.db"), reset=True)
        self.svc = InspectionService(StubDetector(seed=7), self.repo)

    def tearDown(self):
        self.svc.close()
        self.repo.close()
        self.tmp.cleanup()

    def test_three_way_routing(self):
        r = self.svc.handle_frame(1, FRAME)
        self.assertEqual(r["created"], 1)
        self.assertEqual(r["review"], 1)
        self.assertEqual(r["discarded"], 1)
        s = self.repo.stats()
        self.assertEqual(s["events"], 2)
        self.assertEqual(s["tickets"], 1)
        self.assertEqual(s["pending_reviews"], 1)

    def test_dedup_same_bucket(self):
        self.svc.handle_frame(1, FRAME)
        r = self.svc.handle_frame(1, FRAME)
        self.assertEqual(r["deduplicated"], 2)
        self.assertEqual(r["created"], 0)
        self.assertEqual(self.repo.stats()["tickets"], 1)
        self.assertEqual(self.repo.stats()["duplicate_guard_hits"], 2)

    def test_model_version_in_key(self):
        self.svc.handle_frame(1, FRAME)
        svc2 = InspectionService(StubDetector(version="stub-2.0", seed=7), self.repo)
        try:
            r = svc2.handle_frame(1, FRAME)
            self.assertEqual(r["created"], 1)   # 换模型允许重新评估
        finally:
            svc2.close()

    def test_time_bucket_boundary(self):
        t = [datetime(2026, 1, 1, 12, 0, 0)]
        svc = InspectionService(StubDetector(seed=1), self.repo, clock=lambda: t[0])
        try:
            self.assertEqual(svc.handle_frame(1, LOW)["review"], 1)
            t[0] = t[0] + timedelta(seconds=301)
            self.assertEqual(svc.handle_frame(1, LOW)["review"], 1)
        finally:
            svc.close()

    def test_detector_failure_degrades_but_business_continues(self):
        svc = InspectionService(StubDetector(fail_rate=1.0), self.repo)
        try:
            r = svc.handle_frame(2, FRAME)
            self.assertTrue(r["degraded"])
            self.assertEqual(self.repo.stats()["degraded_calls"], 1)
            self.assertEqual(svc.manual_report(2, "rust")["created"], 1)
            self.assertEqual(self.repo.stats()["tickets"], 1)
        finally:
            svc.close()

    def test_transaction_rollback_on_ticket_failure(self):
        with patch.object(self.repo, "create_ticket",
                          side_effect=RuntimeError("simulated db failure")):
            with self.assertRaises(RuntimeError):
                self.svc.handle_frame(1, FRAME)
        self.assertEqual(self.repo.stats()["events"], 0,
                         "事件与工单必须同事务回滚")
        self.assertEqual(self.repo.stats()["tickets"], 0)

    def test_review_confirm_creates_ticket(self):
        self.svc.handle_frame(1, LOW)
        rid = self.repo.list_reviews()[0]["id"]
        out = self.repo.resolve_review(rid, "confirm", "operator_a")
        self.assertEqual(out["status"], "CONFIRMED")
        self.assertIsNotNone(out["ticket_id"])
        self.assertEqual(self.repo.stats()["tickets"], 1)

    def test_reject_feeds_false_positive_export(self):
        self.svc.handle_frame(1, LOW)
        rid = self.repo.list_reviews()[0]["id"]
        self.repo.resolve_review(rid, "reject", "operator_a", note="误报：阴影")
        fp = self.repo.false_positives()
        self.assertEqual(len(fp), 1)
        self.assertEqual(fp[0]["cls"], "lane_occupied")
        self.assertEqual(self.repo.stats()["tickets"], 0)


if __name__ == "__main__":
    unittest.main()
