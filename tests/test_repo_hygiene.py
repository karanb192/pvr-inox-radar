# Repo-wide hygiene (SPEC R121, R122): the em/en dash scanner over
# git-tracked files and the attribution-header check on adapted scripts.

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402

EN_DASH = b"\xe2\x80\x93"  # U+2013
EM_DASH = b"\xe2\x80\x94"  # U+2014

EXEMPT_PREFIX = os.path.join("tests", "fixtures") + os.sep

# Fallback walk (no git available): mirror what the git listing would skip.
WALK_SKIP_DIRS = {".git", ".build", "__pycache__"}


def repo_files():
    """Relative paths of tracked plus untracked-unignored files, so a file
    added in this change set is scanned before it is ever committed."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=context.REPO_ROOT, capture_output=True, text=True,
            timeout=30)
        if proc.returncode == 0 and proc.stdout.strip():
            return [line for line in proc.stdout.splitlines() if line]
    except (OSError, subprocess.SubprocessError):
        pass
    found = []
    for root, dirs, names in os.walk(context.REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in WALK_SKIP_DIRS]
        for name in names:
            if name == ".DS_Store":
                continue
            path = os.path.join(root, name)
            found.append(os.path.relpath(path, context.REPO_ROOT))
    return found


class TestDashScanner(unittest.TestCase):
    def test_no_em_or_en_dashes_in_authored_files(self):
        """R121 / R1: no U+2013 or U+2014 anywhere we author. Only the raw
        third-party captures under tests/fixtures/ are exempt."""
        files = repo_files()
        self.assertGreater(len(files), 10, "file listing looks broken")
        offenders = []
        for rel in files:
            if rel.startswith(EXEMPT_PREFIX):
                continue
            path = os.path.join(context.REPO_ROOT, rel)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            for needle, label in ((EN_DASH, "U+2013"), (EM_DASH, "U+2014")):
                index = data.find(needle)
                if index != -1:
                    line = data.count(b"\n", 0, index) + 1
                    offenders.append("%s line %d: %s" % (rel, line, label))
        self.assertEqual(offenders, [],
                         "dash characters found:\n" + "\n".join(offenders))

    def test_fixture_exemption_is_narrow(self):
        """The exemption covers exactly the raw captures, nothing else."""
        self.assertTrue("tests/fixtures/city.json".replace(
            "/", os.sep).startswith(EXEMPT_PREFIX))
        self.assertFalse("tests/test_render.py".replace(
            "/", os.sep).startswith(EXEMPT_PREFIX))
        self.assertFalse(os.path.join(
            "skills", "pvr-inox-radar", "SKILL.md").startswith(EXEMPT_PREFIX))


class TestAttributionHeaders(unittest.TestCase):
    def test_adapted_scripts_credit_the_reference(self):
        """R122 / R2: every script carrying adapted pvr-inox-mcp logic
        opens with the credit header, within its first 5 lines."""
        for name in ("pvr_client.py", "radar.py", "selftest.py"):
            path = os.path.join(context.SCRIPTS_DIR, name)
            with open(path) as fh:
                head = "".join(fh.readline() for _ in range(5))
            self.assertIn("notprashanth/pvr-inox-mcp", head, name)
            self.assertIn("MIT", head, name)
            self.assertIn("Prashanth Krishnan", head, name)

    def test_original_scripts_still_point_at_the_credits(self):
        """geocode.py and render_map.py are original but must reference the
        project credit rather than implying independent discovery."""
        for name in ("geocode.py", "render_map.py"):
            path = os.path.join(context.SCRIPTS_DIR, name)
            with open(path) as fh:
                self.assertIn("notprashanth/pvr-inox-mcp", fh.read(), name)


class TestSelftestScript(unittest.TestCase):
    def test_selftest_passes_offline(self):
        """scripts/selftest.py is the post-install verification; a break in
        it must fail this suite, not first surface on a user's machine. It
        is offline by design, so running it here touches no network."""
        proc = subprocess.run(
            [sys.executable,
             os.path.join(context.SCRIPTS_DIR, "selftest.py")],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("all checks passed", proc.stdout)
        self.assertNotIn("FAIL", proc.stdout)


if __name__ == "__main__":
    unittest.main()
