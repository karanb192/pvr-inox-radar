# Test bootstrap (SPEC R9): inserts the skill scripts directory at
# sys.path[0] so every test module imports pvr_client, radar, geocode, and
# render_map exactly as the installed skill does. Also exposes the shared
# repo paths and small fixture loaders.

import json
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(REPO_ROOT, "skills", "pvr-inox-radar", "scripts")
ASSETS_DIR = os.path.join(REPO_ROOT, "skills", "pvr-inox-radar", "assets")
FIXTURES_DIR = os.path.join(TESTS_DIR, "fixtures")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


def load_fixture(name):
    """One captured raw API fixture from tests/fixtures/ as a dict."""
    with open(os.path.join(FIXTURES_DIR, name)) as fh:
        return json.load(fh)


def load_asset(name):
    """One authored sample file from the skill's assets/ as a dict."""
    with open(os.path.join(ASSETS_DIR, name)) as fh:
        return json.load(fh)
