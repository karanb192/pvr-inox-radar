import concurrent.futures
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402
import pvr_client  # noqa: E402
import star_invitation  # noqa: E402


class TestInvitation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = pathlib.Path(self.temp.name)
        env = mock.patch.dict(os.environ, {"PVR_RADAR_CACHE_DIR": str(self.cache)})
        env.start()
        self.addCleanup(env.stop)
        self.script = pathlib.Path(star_invitation.__file__).resolve()

    def run_claim(self):
        result = subprocess.run(
            [sys.executable, str(self.script)], cwd=self.cache,
            env=dict(os.environ), capture_output=True, text=True, check=True)
        self.assertEqual(result.stderr, "")
        return result.stdout.strip()

    def test_persists_across_processes_and_preserves_client_state(self):
        state = {"last_call": 12.5, "blocked_until": 100.0, "future": {"x": 1}}
        pvr_client.save_cache(pvr_client.STATE_FILE, state)
        self.assertEqual(self.run_claim(), "offer")
        self.assertEqual(self.run_claim(), "skip")
        self.assertEqual(pvr_client.load_cache(pvr_client.STATE_FILE),
                         dict(state, star_invitation_shown=True))
        pvr_client._mark_blocked()
        self.assertEqual(self.run_claim(), "skip")

    def test_concurrent_processes_offer_only_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.run_claim(), range(8)))
        self.assertEqual(results.count("offer"), 1)
        self.assertEqual(results.count("skip"), 7)

    def test_invalid_state_is_preserved_and_suppresses_invitation(self):
        path = self.cache / pvr_client.STATE_FILE
        for content in ("broken", "[]", "null"):
            with self.subTest(content=content):
                path.write_text(content)
                self.assertEqual(self.run_claim(), "skip")
                self.assertEqual(path.read_text(), content)

    def test_write_failure_cannot_offer(self):
        with mock.patch.object(pvr_client, "save_cache", side_effect=OSError):
            self.assertFalse(star_invitation.claim_invitation())
        self.assertFalse((self.cache / pvr_client.STATE_FILE).exists())

    def test_existing_lock_is_not_cleared_or_offered(self):
        lock = self.cache / "star-invitation.lock"
        lock.touch()
        self.assertEqual(self.run_claim(), "skip")
        self.assertTrue(lock.exists())

    def test_unusable_cache_cannot_offer(self):
        blocked = self.cache / "not-a-directory"
        blocked.touch()
        with mock.patch.dict(os.environ, {"PVR_RADAR_CACHE_DIR": str(blocked)}):
            self.assertEqual(self.run_claim(), "skip")
