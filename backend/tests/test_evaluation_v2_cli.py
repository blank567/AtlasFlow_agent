"""HTTP CLI safety checks without connecting to a backend."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def cli(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "evaluation_v2_cli", Path(__file__).resolve().parents[2] / "scripts" / "evaluate_v2.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return httpx.Response(200, json={"ready": True}, request=httpx.Request(method, url))

    monkeypatch.setattr(module.httpx, "Client", Client)
    return module.main, calls


@pytest.mark.parametrize("kind", ["rag", "agent"])
def test_delete_requires_confirmation_before_any_http(cli, kind):
    main, calls = cli
    with pytest.raises(SystemExit) as exc:
        main(["delete", "--kind", kind, "--experiment", "fixture"])
    assert exc.value.code == 2 and calls == []
    assert main(["delete", "--kind", kind, "--experiment", "fixture", "--confirm-delete"]) == 0
    assert calls[0][:2] == ("DELETE", f"http://127.0.0.1:8000/api/v1/evaluations/{kind}/experiments/fixture")


@pytest.mark.parametrize("frozen", [{"mode": "replay"}, {"input_refs": {"R1:a:1": "old"}}])
def test_rag_frozen_configuration_rejected_locally(cli, tmp_path, frozen):
    main, calls = cli
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"kind": "rag", **frozen}), encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["preflight", "--kind", "rag", "--plan", str(path)])
    assert calls == []


def test_rag_preflight_is_safe_without_live_confirmation(cli, tmp_path):
    main, calls = cli
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"kind": "rag", "mode": "live", "confirm_live": False}), encoding="utf-8")
    assert main(["preflight", "--kind", "rag", "--plan", str(path)]) == 0
    with pytest.raises(SystemExit):
        main(["run", "--kind", "rag", "--plan", str(path)])
    assert len(calls) == 1 and calls[0][1].endswith("/preflight")
