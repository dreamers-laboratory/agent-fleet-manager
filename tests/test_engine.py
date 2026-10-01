"""State-machine tests. Run: python -m pytest tests/ (or python tests/test_engine.py)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentfleet import engine  # noqa: E402


def make_fleet(tmp):
    conn = engine.connect(os.path.join(tmp, "fleet.db"))
    engine.add_source(conn, "alpha", "route-a", cadence_seconds=3600)
    engine.add_source(conn, "beta", "route-b", cadence_seconds=3600)
    return conn


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.tmp = self.tempdir.name
        self.conn = make_fleet(self.tmp)
        self.scopes = os.path.join(self.tmp, "scopes")

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def sweep(self, executor):
        engine.queue_due(self.conn)
        manifest = engine.lease(self.conn, self.scopes)
        for batch in manifest:
            engine.run_batch(self.conn, batch, executor)
            engine.reconcile(self.conn, batch["batch_id"])
        return manifest

    def states(self):
        return [r["state"] for r in self.conn.execute("SELECT state FROM action ORDER BY action_id")]

    def test_success_path(self):
        self.sweep(lambda route: ("ok", b"content-" + route.encode()))
        self.assertEqual(self.states(), ["DONE", "DONE"])
        for row in self.conn.execute("SELECT * FROM source"):
            self.assertEqual(row["error_streak"], 0)
            self.assertIsNotNone(row["last_sha256"])
            self.assertGreater(row["next_due_at"], engine.now_iso())

    def test_due_gating(self):
        self.sweep(lambda route: ("ok", b"x"))
        # Sources were just checked, so nothing is due and nothing queues.
        self.assertEqual(engine.queue_due(self.conn), 0)

    def test_no_duplicate_queue_for_inflight_source(self):
        engine.queue_due(self.conn)
        self.conn.execute("UPDATE source SET next_due_at='2000-01-01T00:00:00Z'")
        self.conn.commit()
        self.assertEqual(engine.queue_due(self.conn), 0)

    def test_change_detection(self):
        self.sweep(lambda route: ("ok", b"v1"))
        self.conn.execute("UPDATE source SET next_due_at='2000-01-01T00:00:00Z'")
        self.conn.commit()
        self.sweep(lambda route: ("ok", b"v1" if route == "route-a" else b"v2"))
        changed = {
            r["name"]: r["changed"]
            for r in self.conn.execute(
                """SELECT s.name, o.changed FROM observation o JOIN source s USING (source_id)
                   WHERE o.observation_id > 2"""
            )
        }
        self.assertEqual(changed, {"alpha": 0, "beta": 1})

    def test_failure_requeues_then_fails_with_backoff(self):
        def failing(route):
            raise RuntimeError("boom")

        for attempt in range(engine.MAX_ATTEMPTS):
            self.conn.execute("UPDATE source SET next_due_at='2000-01-01T00:00:00Z'")
            self.conn.commit()
            self.sweep(failing)
        self.assertEqual(self.states(), ["FAILED", "FAILED"])
        for row in self.conn.execute("SELECT * FROM source"):
            self.assertEqual(row["error_streak"], engine.MAX_ATTEMPTS)

    def test_workers_only_touch_their_scope(self):
        engine.queue_due(self.conn)
        manifest = engine.lease(self.conn, self.scopes, batch_size=1)
        self.assertEqual(len(manifest), 2)
        engine.run_batch(self.conn, manifest[0], lambda route: ("ok", b"x"))
        scope0 = set(os.listdir(manifest[0]["write_scope"]))
        scope1 = set(os.listdir(manifest[1]["write_scope"]))
        self.assertTrue(scope0)
        self.assertFalse(scope1)

    def test_change_log_has_actor_on_every_write(self):
        self.sweep(lambda route: ("ok", b"x"))
        rows = self.conn.execute("SELECT actor, reason FROM change_log").fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["actor"])
            self.assertTrue(row["reason"])


if __name__ == "__main__":
    unittest.main()
