import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from sc2_harness import __main__ as cli
from sc2_harness import runtime
from sc2_harness.runtime import RunManager
from sc2_harness.settings import Settings


def prepared_manager(tmp_path):
    manager = RunManager(Settings(tmp_path))
    run_id = "a" * 32
    manager.store.put({"id": run_id, "status": "completed", "metrics": {"rounds": 1},
                       "config": {"name": "中文证据", "seed": 7}})
    manager.store.append("events", run_id, {"type": "test", "message": "第一条"})
    manager.store.append("snapshots", run_id, {"round": 1, "observation": {"engine": "sandbox"}})
    return manager, run_id


def test_export_publishes_complete_unicode_evidence_and_cleans_staging_files(tmp_path):
    manager, run_id = prepared_manager(tmp_path)
    manager.export(run_id)
    directory = manager.store.run_dir(run_id)
    assert json.loads((directory / "config.json").read_text(encoding="utf-8"))["name"] == "中文证据"
    assert json.loads((directory / "metrics.json").read_text())["rounds"] == 1
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["message"] == "第一条" and events[0]["seq"] > 0
    assert not list(directory.glob(".*.tmp"))


def test_failed_staging_keeps_previous_downloads_intact(tmp_path, monkeypatch):
    manager, run_id = prepared_manager(tmp_path)
    manager.export(run_id)
    directory = manager.store.run_dir(run_id)
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    changed = manager.get(run_id)
    changed["metrics"]["rounds"] = 2
    manager.store.put(changed)
    original_dump = runtime.json.dump

    def fail_metrics(value, stream, **kwargs):
        if isinstance(value, dict) and value.get("rounds") == 2:
            stream.write('{"partial":')
            raise OSError("injected disk write failure")
        return original_dump(value, stream, **kwargs)

    monkeypatch.setattr(runtime.json, "dump", fail_metrics)
    with pytest.raises(OSError, match="injected"):
        manager.export(run_id)
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == before
    assert not list(directory.glob(".*.tmp"))


def test_parallel_exports_are_serialized_and_publish_latest_snapshot(tmp_path, monkeypatch):
    manager, run_id = prepared_manager(tmp_path)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    original_replace = runtime.os.replace
    publishing_threads = []
    first_thread = None

    def blocked_replace(source, target):
        nonlocal first_thread
        identity = threading.get_ident()
        publishing_threads.append(identity)
        if first_thread is None:
            first_thread = identity
            entered.set()
            assert release.wait(5)
        return original_replace(source, target)

    monkeypatch.setattr(runtime.os, "replace", blocked_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(manager.export, run_id)
        assert entered.wait(5)
        changed = manager.get(run_id)
        changed["metrics"]["rounds"] = 2
        manager.store.put(changed)
        manager.store.append("events", run_id, {"type": "test", "message": "第二条"})

        def second_export():
            second_started.set()
            manager.export(run_id)

        second = executor.submit(second_export)
        assert second_started.wait(5)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert publishing_threads[:5] == [first_thread] * 5
    assert len(set(publishing_threads[5:])) == 1 and publishing_threads[5] != first_thread
    directory = manager.store.run_dir(run_id)
    assert json.loads((directory / "run.json").read_text(encoding="utf-8"))["metrics"]["rounds"] == 2
    assert len((directory / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert not list(directory.glob(".*.tmp"))


def test_cli_preserves_completed_trials_when_next_creation_connection_fails(tmp_path, monkeypatch):
    output = tmp_path / "comparison.json"
    run_id = "b" * 32

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, path, *, json):
            if json["seed"] == 19:
                raise httpx.ConnectError("workbench unreachable")
            return httpx.Response(201, json={"id": run_id, "config": json, "status": "completed",
                                  "result": "success", "metrics": {"model_calls": 3}, "error": None,
                                  "observation": {"engine": "sc2"}},
                                  request=httpx.Request("POST", "http://localhost" + path))

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: FakeClient())
    monkeypatch.setattr("sys.argv", ["sc2-harness", "evaluate", "--backend", "sc2", "--policy", "llm",
                                     "--seeds", "7,19", "--output", str(output)])
    with pytest.raises(httpx.ConnectError):
        cli.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["complete"] is False and report["planned_sample_count"] == 2
    assert report["successes"] == 1 and report["runs"][0]["id"] == run_id
    assert report["runs"][0]["real_model_evidence"] is True
    assert report["runs"][1]["status"] == "unconfirmed" and report["runs"][1]["result"] is None
    assert "unreachable" in report["runs"][1]["orchestration_error"]
    assert not list(tmp_path.glob(".*.tmp"))


def test_cli_empty_report_does_not_claim_game_or_model_evidence(tmp_path):
    class Args:
        backend, policy = "sc2", "llm"
        output = tmp_path / "empty.json"

    cli._write_report(Args, [], planned_count=3, complete=False)
    report = json.loads(Args.output.read_text(encoding="utf-8"))
    assert report["sample_count"] == 0 and report["complete"] is False
    assert report["native_game_evidence"] is False and report["real_model_evidence"] is False
