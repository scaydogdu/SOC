"""Explicitly opted-in paid M1 integration test."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.nightly
def test_live_roster_and_toy_record_replay():
    output = os.environ.get("SOC_M1_LIVE_OUTPUT")
    if not output:
        pytest.skip("set SOC_M1_LIVE_OUTPUT to opt into the paid M1 campaign")
    root = Path(__file__).resolve().parents[2]
    config = os.environ.get("SOC_M1_LIVE_CONFIG", "config/m1-live.yaml")
    for command in ("probe", "toy"):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "soc.orchestrator.live",
                command,
                "--output",
                output,
                "--config",
                config,
            ],
            cwd=root,
            check=True,
        )
