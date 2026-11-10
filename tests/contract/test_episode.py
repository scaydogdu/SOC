import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from eval.episode import EpisodeError, main, validate
from soc.episode import load_runtime

TOY = Path("data/episodes/toy")


def test_runtime_reads_only_public_files(tmp_path):
    public = {"source.jsonl", "brief.yaml", "events.yaml"}
    for name in public:
        shutil.copyfile(TOY / name, tmp_path / name)
    result = load_runtime(tmp_path).model_dump_json()
    for private in (
        "expected_answer",
        "affected",
        "Keep examples terse",
        "planted_errors",
        "P1",
    ):
        assert private not in result

    # Python's open audit event covers builtins.open, io.open and os.open.
    script = """
from pathlib import Path
import os
import sys
from soc.episode import load_runtime

episode = Path(sys.argv[1]).resolve()
allowed = {"source.jsonl", "brief.yaml", "events.yaml"}
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.parent == episode:
            assert path.name in allowed, f"Private episode file opened: {path}"
sys.addaudithook(audit)
load_runtime(episode)
"""
    subprocess.run([sys.executable, "-c", script, str(TOY)], check=True)


def test_valid_episode(monkeypatch, capsys):
    assert len(validate(TOY).hidden.preferences) == 5
    monkeypatch.setattr("sys.argv", ["episode", str(TOY)])
    main()
    assert "Valid episode:" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("rule", "file", "edit"),
    [
        ("manifest", "source.jsonl", lambda s: s + "\n"),
        ("sentence IDs", "source.jsonl", lambda s: s + s.splitlines()[0] + "\n"),
        (
            "sentence IDs",
            "seeds.jsonl",
            lambda s: s.replace("9999-1-s03", "9999-1-s99"),
        ),
        ("brief clause IDs", "hidden.yaml", lambda s: s.replace("B1", "B99")),
        (
            "event positions",
            "events.yaml",
            lambda s: s.replace("executor_change_after: 2", "executor_change_after: 3"),
        ),
        (
            "event positions",
            "events.yaml",
            lambda s: s.replace("executor_change_after: 2", "executor_change_after: 9"),
        ),
        (
            "coverage",
            "events.yaml",
            lambda s: s.replace(
                "scope_keys:\n  - refresh token", "scope_keys:\n  - absent"
            ),
        ),
        (
            "coverage",
            "events.yaml",
            lambda s: s.replace("kind: scope_limited", "kind: term"),
        ),
    ],
)
def test_broken_copies_fail_the_expected_rule(tmp_path, rule, file, edit):
    directory = tmp_path / "episode"
    shutil.copytree(TOY, directory)
    target = directory / file
    target.write_text(edit(target.read_text()))
    if rule != "manifest":
        path = directory / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["sha256"][file] = hashlib.sha256(target.read_bytes()).hexdigest()
        path.write_text(json.dumps(manifest))
    with pytest.raises(EpisodeError, match=f"^{rule}:"):
        validate(directory)


def test_runtime_rejects_extra_schedule_fields(tmp_path):
    directory = tmp_path / "episode"
    shutil.copytree(TOY, directory)
    path = directory / "events.yaml"
    data = yaml.safe_load(path.read_text())
    data["approvals"][0]["expected_answer"] = "private"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="expected_answer"):
        load_runtime(directory)


@pytest.mark.parametrize(
    ("rule", "file", "edit"),
    [
        ("sentence IDs", "events.yaml", lambda s: s.replace("  - 9999-1-s02\n", "")),
        (
            "event positions",
            "affected.jsonl",
            lambda s: s.replace('"chunk": 4', '"chunk": 3'),
        ),
        (
            "event positions",
            "hidden.yaml",
            lambda s: s.replace("chunk: 2", "chunk: 4"),
        ),
        (
            "coverage",
            "events.yaml",
            lambda s: s.replace("- decision_id: D5", "- decision_id: DX", 1),
        ),
        ("coverage", "affected.jsonl", lambda s: s.splitlines()[0] + "\n"),
        (
            "coverage",
            "affected.jsonl",
            lambda s: (
                s.replace('"repair"', '"tmp"')
                .replace('"apply"', '"repair"')
                .replace('"tmp"', '"apply"')
            ),
        ),
        (
            "coverage",
            "affected.jsonl",
            lambda s: s.replace("9999-2-s01", "9999-2-s02", 1),
        ),
        (
            "event positions",
            "events.yaml",
            lambda s: s.replace(
                "executor_change_after:", "- sentence_ids: []\nexecutor_change_after:"
            ),
        ),
        (
            "coverage",
            "events.yaml",
            lambda s: s.replace("- decision_id: D2", "- decision_id: D1"),
        ),
        (
            "coverage",
            "events.yaml",
            lambda s: s.replace("- decision_id: D5", "- decision_id: D1", 1),
        ),
    ],
)
def test_additional_cross_file_rules(tmp_path, rule, file, edit):
    directory = tmp_path / "episode"
    shutil.copytree(TOY, directory)
    target = directory / file
    target.write_text(edit(target.read_text()))
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["sha256"][file] = hashlib.sha256(target.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(EpisodeError, match=f"^{rule}:"):
        validate(directory)


@pytest.mark.parametrize("bad_file", ["extra", "missing hash", "invalid JSON"])
def test_manifest_format(tmp_path, monkeypatch, capsys, bad_file):
    directory = tmp_path / "episode"
    shutil.copytree(TOY, directory)
    manifest = directory / "manifest.json"
    if bad_file == "extra":
        (directory / "extra.txt").touch()
    elif bad_file == "missing hash":
        manifest.write_text('{"sha256": {}}')
    else:
        manifest.write_text("{")
    monkeypatch.setattr("sys.argv", ["episode", str(directory)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("manifest:")


def test_finder_metadata_is_ignored(tmp_path):
    shutil.copytree(TOY, tmp_path, dirs_exist_ok=True)
    (tmp_path / ".DS_Store").write_bytes(b"Finder metadata")
    assert validate(tmp_path)


@pytest.mark.parametrize(("location", "value"), [("approval", " "), ("revision", None)])
def test_decision_content_required(tmp_path, location, value):
    shutil.copytree(TOY, tmp_path, dirs_exist_ok=True)
    path = tmp_path / "events.yaml"
    data = yaml.safe_load(path.read_text())
    record = (
        data["approvals"][0]
        if location == "approval"
        else data["brief_change"]["revisions"][0]
    )
    if value is None:
        record.pop("value")
    else:
        record["value"] = value
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="value"):
        load_runtime(tmp_path)
