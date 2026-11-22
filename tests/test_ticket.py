import unittest
from datetime import datetime, timedelta

from app import ticket as tm


class TicketStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.row = {
            "id": 1, "status": "OPEN", "assignee": None,
            "sla_due_at": (datetime.now() + timedelta(hours=1)).isoformat(),
        }

    def test_legal_path(self):
        r = tm.transition(self.row, "CLAIM", "operator_a")
        self.assertEqual(r["status"], "CLAIMED")
        self.assertEqual(r["assignee"], "operator_a")
        row2 = dict(self.row, **r)
        self.assertEqual(tm.transition(row2, "RESOLVE", "operator_a")["status"],
                         "RESOLVED")

    def test_illegal_transition_rejected(self):
        with self.assertRaises(ValueError):
            tm.transition(self.row, "RESOLVE", "operator_a")
        with self.assertRaises(ValueError):
            tm.transition(dict(self.row, status="CLOSED"), "CLAIM", "admin")

    def test_overdue(self):
        self.assertFalse(tm.is_overdue(self.row))
        past = dict(self.row,
                    sla_due_at=(datetime.now() - timedelta(hours=1)).isoformat())
        self.assertTrue(tm.is_overdue(past))
        closed = dict(past, status="CLOSED")
        self.assertFalse(tm.is_overdue(closed))


if __name__ == "__main__":
    unittest.main()
