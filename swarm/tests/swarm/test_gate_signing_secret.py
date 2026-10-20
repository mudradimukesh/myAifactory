import importlib
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECRET = "INTERIOR_IMAGE_URL_SIGNING_SECRET"


@pytest.fixture
def evidence(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("gate_evidence")
    owned = importlib.import_module("owned_processes")
    listener = MagicMock()
    listener.getsockname.return_value = ("127.0.0.1", 8765)
    listener.__enter__.return_value = listener
    monkeypatch.setattr(module.socket, "socket", Mock(return_value=listener))
    monkeypatch.setattr(module.subprocess, "run", Mock())
    child = Mock(pid=12345)
    child.poll.return_value = None
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        Mock(
            side_effect=lambda *args, **kwargs: (kwargs["stdout"].write("API startup\n"), child)[1]
        ),
    )
    response = MagicMock()
    response.__enter__.return_value.status = 200
    monkeypatch.setattr(module.urllib.request, "urlopen", Mock(return_value=response))
    monkeypatch.setattr(owned, "capture", Mock())
    monkeypatch.setattr(owned, "stop", Mock(return_value=([], [])))
    return module


@pytest.mark.parametrize("env", [{}, {SECRET: ""}, {SECRET: "inherited-secret"}])
def test_runtime_supplies_nonempty_signing_secret(
    tmp_path: Path, evidence: ModuleType, env: dict[str, str]
) -> None:
    with evidence.runtime(tmp_path, tmp_path, "secret", env) as configured:
        assert configured.get(SECRET), "The isolated API needs a nonempty image URL signing secret"
        assert evidence.subprocess.Popen.call_args.kwargs["env"][SECRET] == configured[SECRET]
        assert evidence.subprocess.run.call_args.kwargs["env"][SECRET] == configured[SECRET]
    assert env == {} or configured[SECRET] != env[SECRET]


def test_runtime_generates_different_signing_secrets(tmp_path: Path, evidence: ModuleType) -> None:
    env = {SECRET: "inherited-secret"}
    with evidence.runtime(tmp_path, tmp_path, "first", env) as first:
        first_secret = first[SECRET]
    with evidence.runtime(tmp_path, tmp_path, "second", env) as second:
        assert first_secret != second[SECRET], (
            "Each gate run needs its own image URL signing secret"
        )
    assert env == {SECRET: "inherited-secret"}


@pytest.mark.parametrize("env", [{}, {SECRET: ""}, {SECRET: "inherited-secret"}])
def test_runtime_generates_signing_secret_with_token_urlsafe_32(
    tmp_path: Path, evidence: ModuleType, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    sentinels = ["first-signing-secret-sentinel", "second-signing-secret-sentinel"]
    generator = Mock(side_effect=sentinels)
    monkeypatch.setattr(evidence.secrets, "token_urlsafe", generator)
    original_env = env.copy()
    generated = []
    for index, sentinel in enumerate(sentinels):
        with evidence.runtime(tmp_path, tmp_path, f"generator-{index}", env) as configured:
            assert generator.call_count == index + 1
            generator.assert_called_with(32)
            assert configured[SECRET] == sentinel
            assert evidence.subprocess.Popen.call_args.kwargs["env"][SECRET] == sentinel
            assert evidence.subprocess.run.call_args.kwargs["env"][SECRET] == sentinel
            generated.append(configured[SECRET])
    assert generated[0] != generated[1]
    assert env == original_env


@pytest.mark.parametrize("startup_fails", [False, True])
def test_runtime_does_not_log_or_record_signing_secret(
    tmp_path: Path, evidence: ModuleType, startup_fails: bool
) -> None:
    if startup_fails:
        evidence.urllib.request.urlopen.side_effect = ValueError("API startup failed")
    configured = None
    try:
        with evidence.runtime(tmp_path, tmp_path, "private", {SECRET: "inherited-secret"}) as env:
            configured = env
    except ValueError as error:
        assert startup_fails
        assert str(error) == "API startup failed"
    api_env = evidence.subprocess.Popen.call_args.kwargs["env"]
    secret = api_env[SECRET]
    assert secret
    assert (configured is None) == startup_fails
    log = (tmp_path / "private-runtime.log").read_text()
    receipt = (tmp_path / "private-runtime.json").read_text()
    assert "API startup" in log
    assert '"owned_processes"' in receipt
    assert secret not in log
    assert secret not in receipt
