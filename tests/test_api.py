import os
import tempfile
import unittest

from ai.detector import StubDetector
from ai.service import InspectionService
from app.main import Api
from app.repo import Repo

ADMIN = {"X-Token": "tk-admin"}
VIEWER = {"X-Token": "tk-view-a"}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(os.path.join(self.tmp.name, "t.db"), reset=True)
        self.svc = InspectionService(StubDetector(seed=3), self.repo)
        self.api = Api(self.repo, self.svc)

    def tearDown(self):
        self.svc.close()
        self.repo.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=ADMIN, query=None):
        return self.api.dispatch(method, path, query or {}, body or {}, headers)

    def test_health_and_login(self):
        self.assertEqual(self.call("GET", "/health")[0], 200)
        code, data = self.call("POST", "/api/login", {"username": "admin"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(data["token"], "tk-admin")
        self.assertEqual(self.call("POST", "/api/login",
                                   {"username": "nobody"}, {})[0], 401)

    def test_devices_and_role_guard(self):
        self.assertEqual(len(self.call("GET", "/api/devices")[1]["items"]), 6)
        with self.assertRaises(PermissionError):
            self.call("POST", "/api/devices",
                      {"code": "X", "name": "X", "area": "A"}, VIEWER)
        code, _ = self.call("POST", "/api/devices",
                            {"code": "X", "name": "新点位", "area": "A"})
        self.assertEqual(code, 201)
        self.assertEqual(len(self.call("GET", "/api/devices")[1]["items"]), 7)

    def test_frame_to_ticket_flow(self):
        code, out = self.call("POST", "/api/frames",
                              {"point_id": 1,
                               "defects": [{"cls": "indicator_abnormal", "score": 0.95}]})
        self.assertEqual(code, 200)
        self.assertEqual(out["created"], 1)
        page = self.call("GET", "/api/tickets", query={"page": ["1"]})[1]
        self.assertEqual(page["total"], 1)
        tid = page["items"][0]["id"]
        self.assertEqual(self.call("POST", "/api/tickets/transition",
                                   {"id": tid, "action": "CLAIM"})[1]["status"],
                         "CLAIMED")

    def test_cross_area_frame_denied(self):
        code, _ = self.call("POST", "/api/frames",
                            {"point_id": 4, "defects": []},
                            {"X-Token": "tk-op-a"})
        self.assertEqual(code, 403)

    def test_review_resolve_validation(self):
        self.assertEqual(self.call("POST", "/api/reviews/resolve",
                                   {"id": 1, "action": "bad"})[0], 400)

    def test_metrics_and_feedback(self):
        self.assertEqual(self.call("GET", "/api/metrics")[1]["devices"], 6)
        self.assertIn("items", self.call("GET", "/api/feedback")[1])

    def test_unknown_route(self):
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
