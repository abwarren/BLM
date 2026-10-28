"""COLLECTOR-HEARTBEAT PATH ALIGNMENT (2026-10-28).

The API reads the collector heartbeat from

    <BLM_DATA_ROOT>/blm_v4/state/collector_state.json      (blm_v4/api.py)

while the collector wrote it to ``<repo>/blm_v4/state/collector_state.json``.
On the production box those coincide (the collector's checkout IS the data
root), so nothing ever noticed.  On the stage box they diverge —
repo ``/home/ubuntu/blm``, ``BLM_DATA_ROOT=/srv/blm/data`` — so a running
collector there would publish its heartbeat where the API never looks and
``/api/v4/status`` would keep serving ``collector: null``.

These tests pin the ONE invariant that makes the two agree for every
deployment shape: both modules resolve the same file, and the whole state
directory (heartbeat + comp_ids) follows the data root, not the code tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROBE = """
import json, os
from blm_v4 import collector
from blm_v4 import api
print(json.dumps({
    "collector_dir":  str(collector.STATE_DIR),
    "collector_file": str(collector.STATE_FILE),
    "comp_ids_file":  str(collector.COMP_IDS_FILE),
    "api_file":       str(api.STATE_FILE),
}))
"""


def _probe(data_root: str | None) -> dict:
    env = dict(os.environ)
    env.pop("BLM_DATA_ROOT", None)
    if data_root is not None:
        env["BLM_DATA_ROOT"] = data_root
    proc = subprocess.run([sys.executable, "-c", PROBE], cwd=str(REPO),
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_collector_writes_the_heartbeat_where_the_api_reads_it(tmp_path):
    """The divergent-root deployment (stage) is the case that was broken."""
    root = tmp_path / "data"
    got = _probe(str(root))
    assert got["collector_file"] == got["api_file"], (
        "collector heartbeat path must equal the path /api/v4/status reads\n"
        f"  collector: {got['collector_file']}\n  api      : {got['api_file']}")
    assert Path(got["collector_file"]) == root / "blm_v4" / "state" / "collector_state.json"


def test_the_whole_state_directory_follows_the_data_root(tmp_path):
    """comp_ids.json is session state too: half the dir must not stay behind."""
    got = _probe(str(tmp_path / "data"))
    assert Path(got["comp_ids_file"]).parent == Path(got["collector_dir"])
    assert Path(got["collector_dir"]) == Path(got["collector_file"]).parent


def test_a_single_root_deployment_keeps_its_previous_path():
    """Prior behaviour when BLM_DATA_ROOT is unset: state beside the code."""
    got = _probe(None)
    assert got["collector_file"] == got["api_file"]
    assert Path(got["collector_file"]) == REPO / "blm_v4" / "state" / "collector_state.json"
