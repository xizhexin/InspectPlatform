import os
import tempfile
import unittest

from app.repo import Repo
from app.scheduler import PlanRunner


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(os.path.join(self.tmp.name, "t.db"), reset=True)
        now = [1000.0]
        self.runner = PlanRunner(self.repo, clock=lambda: now[0])
        self.now = now

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_no_duplicate_run(self):
        self.assertEqual(self.runner.tick(lambda p: p["device_id"])["due"], 6)
        self.assertEqual(self.runner.tick(lambda p: p["device_id"])["due"], 0)

    def test_interval_elapsed_runs_again(self):
        self.runner.tick()
        self.now[0] += 301
        self.assertEqual(self.runner.tick()["due"], 6)

    def test_stale_plan_recovery(self):
        self.repo.claim_plan(self.now[0])          # 模拟进程崩溃，未 release
        self.assertEqual(self.runner.tick()["due"], 0)
        self.now[0] += 601
        self.assertEqual(self.runner.recover(max_age_s=600), 6)
        self.assertEqual(self.runner.tick()["due"], 6)

    def test_callback_failure_releases_plan(self):
        def boom(plan):
            raise RuntimeError("capture failed")
        self.assertEqual(self.runner.tick(boom)["due"], 6)
        self.now[0] += 301
        self.assertEqual(self.runner.tick()["due"], 6,
                         "单次失败不能让计划永久卡在 running")


if __name__ == "__main__":
    unittest.main()
