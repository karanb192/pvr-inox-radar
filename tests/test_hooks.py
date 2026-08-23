# The plugin's UserPromptSubmit routing hook: two real sessions (23 Aug
# 2026, Haiku) answered movie asks from memory and recommended competitor
# websites while this skill sat installed. The hook makes the nudge
# deterministic: movie-shaped prompts get one line of context naming the
# skill, everything else gets silence. Offline subprocess tests.

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402

HOOK = os.path.join(context.REPO_ROOT, "hooks", "route.py")
HOOKS_JSON = os.path.join(context.REPO_ROOT, "hooks", "hooks.json")


def run_hook(payload):
    proc = subprocess.run([sys.executable, HOOK], input=payload,
                          capture_output=True, text=True, timeout=30)
    return proc


class TestRoutingHook(unittest.TestCase):
    REAL_MISSES = [
        "What are the top rated movies we can watch near indiranagar - "
        "recliner - 5 seats",
        "What recliners are available near Sector 56 Gurgaon tonight, "
        "2 seats together, cheapest first?",
    ]

    def test_both_real_world_misses_now_trigger_the_nudge(self):
        for prompt in self.REAL_MISSES:
            proc = run_hook(json.dumps({"user_prompt": prompt}))
            self.assertEqual(proc.returncode, 0)
            self.assertIn("pvr-inox-radar", proc.stdout, prompt)

    def test_legacy_prompt_key_also_matches(self):
        proc = run_hook(json.dumps({"prompt": self.REAL_MISSES[0]}))
        self.assertIn("pvr-inox-radar", proc.stdout)

    def test_unrelated_prompts_stay_silent(self):
        for prompt in ("refactor the auth middleware",
                       "summarize this PDF for me",
                       "what is the capital of France"):
            proc = run_hook(json.dumps({"prompt": prompt}))
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "", prompt)

    def test_garbage_stdin_never_blocks_the_prompt(self):
        for payload in ("", "not json", "{\"prompt\": 5}"):
            proc = run_hook(payload)
            self.assertEqual(proc.returncode, 0, payload)

    def test_nudge_is_one_line_and_dash_clean(self):
        proc = run_hook(json.dumps({"prompt": "movies tonight?"}))
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("\u2014", proc.stdout)
        self.assertNotIn("\u2013", proc.stdout)


class TestHooksManifest(unittest.TestCase):
    def test_hooks_json_is_valid_and_points_at_the_script(self):
        with open(HOOKS_JSON) as fh:
            manifest = json.load(fh)
        text = json.dumps(manifest)
        self.assertIn("UserPromptSubmit", text)
        self.assertIn("route.py", text)
        self.assertIn("CLAUDE_PLUGIN_ROOT", text)


if __name__ == "__main__":
    unittest.main()
