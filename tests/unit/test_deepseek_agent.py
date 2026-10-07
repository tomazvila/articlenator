from __future__ import annotations

import importlib.util
import json
import os
import ssl
import urllib.error
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "zettel_ralph" / "deepseek_agent.py"
SPEC = importlib.util.spec_from_file_location("deepseek_agent", MODULE_PATH)
deepseek_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(deepseek_agent)


def test_resolve_allowed_path_rejects_paths_outside_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("ZR_STAGING", str(tmp_path / "staging"))

    with pytest.raises(deepseek_agent.AgentError):
        deepseek_agent.resolve_allowed_path("/etc/passwd")


def test_resolve_allowed_path_accepts_configured_vault(tmp_path, monkeypatch):
    # Round 5: the read roots are the zettelkasten folders of $ZK_DIR, not the vault root.
    zk = tmp_path / "vault" / "ZK"
    monkeypatch.setenv("VAULT", str(zk.parent))
    monkeypatch.setenv("ZK_DIR", str(zk))
    monkeypatch.setenv("ZR_STAGING", str(tmp_path / "staging"))

    resolved = deepseek_agent.resolve_allowed_path(str(zk / "01 Permanent Notes" / "A.md"))

    assert resolved == (zk / "01 Permanent Notes" / "A.md").resolve()
    with pytest.raises(deepseek_agent.AgentError):
        deepseek_agent.resolve_allowed_path(str(zk.parent / "Other Note.md"))


def test_helper_path_rejects_non_helper():
    with pytest.raises(deepseek_agent.AgentError):
        deepseek_agent.helper_path("deepseek_agent.py")


def test_run_command_allows_pwd_only_without_args():
    result = deepseek_agent.run_command("pwd")

    assert result["returncode"] == 0
    assert result["stdout"].endswith("zettel_ralph\n")


def test_run_command_rejects_shell_commands():
    with pytest.raises(deepseek_agent.AgentError):
        deepseek_agent.run_command("bash", ["loop.sh"])


def test_load_api_key_prefers_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    assert deepseek_agent.load_api_key() == "sk-test"


def test_load_api_key_reads_file(tmp_path, monkeypatch):
    key_file = tmp_path / "deepseek.txt"
    key_file.write_text("sk-file\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(key_file))

    assert deepseek_agent.load_api_key() == "sk-file"


def _chat_kwargs():
    return {
        "api_key": "sk-test",
        "base_url": "https://example.invalid/api/v1",
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "timeout_seconds": 5,
    }


def test_chat_completion_retries_transient_connection_error(monkeypatch):
    import ssl

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    calls = []
    outcomes = [
        urllib.error.URLError(ssl.SSLEOFError("EOF occurred in violation")),
        urllib.error.URLError(ConnectionResetError("connection reset")),
        {"choices": [{"message": {"content": "ok"}}]},
    ]

    def fake_urlopen(request, timeout):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(deepseek_agent.urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    monkeypatch.setattr(deepseek_agent.time, "sleep", sleeps.append)

    result = deepseek_agent.chat_completion(**_chat_kwargs())

    assert result["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 3
    assert sleeps == [2, 4]


def test_chat_completion_does_not_retry_http_error(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", hdrs=None, fp=None
        )

    monkeypatch.setattr(deepseek_agent.urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    monkeypatch.setattr(deepseek_agent.time, "sleep", sleeps.append)

    with pytest.raises(deepseek_agent.AgentError, match="HTTP 401"):
        deepseek_agent.chat_completion(**_chat_kwargs())

    assert len(calls) == 1
    assert sleeps == []


def test_chat_completion_gives_up_after_transient_attempts(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise urllib.error.URLError(ssl.SSLError("handshake failure"))

    monkeypatch.setattr(deepseek_agent.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(deepseek_agent.time, "sleep", lambda _s: None)

    with pytest.raises(deepseek_agent.AgentError, match="connection error"):
        deepseek_agent.chat_completion(**_chat_kwargs())

    assert len(calls) == deepseek_agent.TRANSIENT_ATTEMPTS


def test_chat_completion_backoff_is_capped(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise urllib.error.URLError(OSError("network is unreachable"))

    monkeypatch.setattr(deepseek_agent.urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    monkeypatch.setattr(deepseek_agent.time, "sleep", sleeps.append)

    with pytest.raises(deepseek_agent.AgentError):
        deepseek_agent.chat_completion(**_chat_kwargs())

    assert sleeps == sorted(sleeps)
    assert max(sleeps) <= deepseek_agent.TRANSIENT_BACKOFF_CAP_S


def test_allowed_roots_include_staging_and_vault(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    vault = tmp_path / "vault"
    monkeypatch.setenv("ZR_STAGING", str(staging))
    monkeypatch.setenv("VAULT", str(vault))

    zk = vault / "ZK"
    monkeypatch.setenv("ZK_DIR", str(zk))
    roots = set(map(os.fspath, deepseek_agent.allowed_roots()))

    # Round 5 (B2): whole folders are the shadow and the lit folders only. The staging
    # and the vault root are not read roots; single files and zettelkasten folders are.
    assert os.fspath(staging.resolve()) not in roots
    assert os.fspath(vault.resolve()) not in roots
    assert deepseek_agent.read_allowed((zk / "01 Permanent Notes" / "A.md").resolve())
    assert deepseek_agent.read_allowed((zk / "Home.md").resolve())
    assert deepseek_agent.read_allowed((staging / "STATE.md").resolve())
    assert not deepseek_agent.read_allowed((staging / "queue.json").resolve())
    assert not deepseek_agent.read_allowed((vault / "Diary.md").resolve())
    assert not deepseek_agent.read_allowed((zk / "03 Reviews" / "R.md").resolve())
