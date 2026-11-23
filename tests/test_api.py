import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from ai.detector import StubDetector
from ai.service import InspectionService
from app.main import create_app
from app.repo import Repo

ADMIN = {"X-Token": "tk-admin"}
VIEWER = {"X-Token": "tk-view-a"}


class ApiTest(unittest.TestCase):
    """走真实 HTTP 层（TestClient），验证路由、鉴权、Pydantic 校验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(os.path.join(self.tmp.name, "t.db"), reset=True)
        self.svc = InspectionService(StubDetector(seed=3), self.repo)
        # 不进 with：避免 lifespan 提前 close repo，生命周期由 tearDown 管
        self.client = TestClient(create_app(self.repo, self.svc))

    def tearDown(self):
        self.client.close()
        self.svc.close()
        self.repo.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=ADMIN, params=None):
        return self.client.request(method, path, json=body, headers=headers,
                                   params=params)

    def test_health_and_login(self):
        self.assertEqual(self.call("GET", "/health", headers={}).status_code, 200)
        r = self.call("POST", "/api/login", {"username": "admin"}, headers={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["token"], "tk-admin")
        self.assertEqual(
            self.call("POST", "/api/login", {"username": "nobody"}, headers={}).status_code,
            401)

    def test_devices_and_role_guard(self):
        self.assertEqual(len(self.call("GET", "/api/devices").json()["items"]), 6)
        self.assertEqual(
            self.call("POST", "/api/devices",
                      {"code": "X", "name": "X", "area": "A"}, VIEWER).status_code,
            403)
        r = self.call("POST", "/api/devices", {"code": "X", "name": "新点位", "area": "A"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(self.call("GET", "/api/devices").json()["items"]), 7)

    def test_frame_to_ticket_flow(self):
        r = self.call("POST", "/api/frames",
                      {"point_id": 1,
                       "defects": [{"cls": "indicator_abnormal", "score": 0.95}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["created"], 1)
        page = self.call("GET", "/api/tickets", params={"page": 1}).json()
        self.assertEqual(page["total"], 1)
        tid = page["items"][0]["id"]
        r = self.call("POST", "/api/tickets/transition", {"id": tid, "action": "CLAIM"})
        self.assertEqual(r.json()["status"], "CLAIMED")

    def test_cross_area_frame_denied(self):
        r = self.call("POST", "/api/frames", {"point_id": 4, "defects": []},
                      {"X-Token": "tk-op-a"})
        self.assertEqual(r.status_code, 403)

    def test_review_resolve_validation(self):
        self.assertEqual(
            self.call("POST", "/api/reviews/resolve", {"id": 1, "action": "bad"}).status_code,
            400)

    def test_invalid_body_rejected_by_pydantic(self):
        # score 超出 [0,1] 由 Pydantic 挡下，返回 400 而不是静默入库
        r = self.call("POST", "/api/frames",
                      {"point_id": 1, "defects": [{"cls": "rust", "score": 1.5}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            self.call("POST", "/api/frames", {"defects": []}).status_code, 400)

    def test_metrics_and_feedback(self):
        self.assertEqual(self.call("GET", "/api/metrics").json()["devices"], 6)
        self.assertIn("items", self.call("GET", "/api/feedback").json())

    def test_unknown_route(self):
        self.assertEqual(self.call("GET", "/api/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
