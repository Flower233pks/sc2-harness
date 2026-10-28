import json

import httpx
import pytest

from sc2_harness import __main__ as cli


@pytest.mark.parametrize("command", ["run", "evaluate"])
@pytest.mark.parametrize("budget", [0, 512, 1024])
def test_cli_reasoning_budget_reaches_api_config_and_report(tmp_path, monkeypatch, command, budget):
    sent = []
    output = tmp_path / "comparison.json"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, path, *, json):
            assert path == "/api/runs"
            sent.append(json)
            return httpx.Response(
                201,
                request=httpx.Request("POST", "http://localhost/api/runs"),
                json={"id": "a" * 32, "config": json, "status": "completed", "result": "incomplete",
                      "metrics": {"model_calls": 0}, "observation": {}},
            )

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: FakeClient())
    argv = ["sc2-harness", command, "--backend", "sandbox", "--policy", "llm", "--seeds", "7",
            "--output", str(output)]
    if budget:
        argv.extend(["--reasoning-budget-tokens", str(budget)])
    monkeypatch.setattr("sys.argv", argv)
    cli.main()
    assert len(sent) == 1 and sent[0]["reasoning_budget_tokens"] == budget
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["reasoning_budget_tokens"] == budget
    assert report["runs"][0]["config"]["reasoning_budget_tokens"] == budget
    assert report["complete"] is True and report["real_model_evidence"] is False


@pytest.mark.parametrize("budget", ["-1", "1025", "true", "1.5", "unbounded"])
def test_cli_invalid_reasoning_budget_is_rejected_before_http_or_report(tmp_path, monkeypatch, budget):
    def no_client(**kwargs):
        raise AssertionError("Invalid CLI config must not access the API")

    monkeypatch.setattr(cli.httpx, "Client", no_client)
    output = tmp_path / "not-created.json"
    monkeypatch.setattr("sys.argv", ["sc2-harness", "run", "--reasoning-budget-tokens", budget,
                                     "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and not output.exists()


@pytest.mark.parametrize("command", ["run", "evaluate"])
def test_cli_reference_guidance_profile_is_explicit_in_api_configuration_and_report(tmp_path, monkeypatch, command):
    sent = []
    output = tmp_path / "reference.json"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, path, *, json):
            assert path == "/api/runs"
            sent.append(json)
            return httpx.Response(201, request=httpx.Request("POST", "http://localhost/api/runs"),
                                  json={"id": "b" * 32, "config": json, "status": "completed",
                                        "result": "incomplete", "metrics": {}, "observation": {}})

    monkeypatch.setattr(cli.httpx, "Client", lambda **_kwargs: FakeClient())
    monkeypatch.setattr("sys.argv", ["sc2-harness", command, "--backend", "sandbox", "--policy", "llm",
                                     "--scenario", "full_match", "--guidance-profile", "marine_reference_v1",
                                     "--seeds", "7", "--output", str(output)])
    cli.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert len(sent) == 1 and sent[0]["guidance_profile"] == "marine_reference_v1"
    assert report["guidance_profile"] == report["runs"][0]["config"]["guidance_profile"] == "marine_reference_v1"
