import os
import tempfile
import unittest

from app.auth import area_filter, authenticate, can_see, require
from app.repo import Repo


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(os.path.join(self.tmp.name, "t.db"), reset=True)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_authenticate(self):
        self.assertIsNone(authenticate(self.repo.conn, "bad-token"))
        u = authenticate(self.repo.conn, "tk-op-a")
        self.assertEqual(u.role, "operator")
        self.assertEqual(u.area, "A")

    def test_role_permission(self):
        viewer = authenticate(self.repo.conn, "tk-view-a")
        with self.assertRaises(PermissionError):
            require(viewer, "device:write")
        with self.assertRaises(PermissionError):
            require(viewer, "export")
        self.assertEqual(require(viewer, "read").username, "viewer_a")
        with self.assertRaises(PermissionError):
            require(None, "read")

    def test_area_scope(self):
        op = authenticate(self.repo.conn, "tk-op-a")
        admin = authenticate(self.repo.conn, "tk-admin")
        self.assertEqual(area_filter(op), "A")
        self.assertIsNone(area_filter(admin))
        self.assertTrue(can_see(op, "A"))
        self.assertFalse(can_see(op, "B"))
        self.assertEqual(len(self.repo.devices(area_filter(op))), 3)

    def test_cross_area_ticket_transition_denied(self):
        op = authenticate(self.repo.conn, "tk-op-a")
        tid = self.repo.create_ticket(4, "rust", "P2")   # 4 号设备在 B 区
        with self.assertRaises(PermissionError):
            self.repo.transition_ticket(tid, "CLAIM", op)


if __name__ == "__main__":
    unittest.main()
