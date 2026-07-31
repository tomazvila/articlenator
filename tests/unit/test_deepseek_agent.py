from __future__ import annotations

import importlib.util
import os
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
    vault = tmp_path / "vault"
    monkeypatch.setenv("VAULT", str(vault))
    monkeypatch.setenv("ZR_STAGING", str(tmp_path / "staging"))

    resolved = deepseek_agent.resolve_allowed_path(str(vault / "01 Permanent Notes" / "A.md"))

    assert resolved == (vault / "01 Permanent Notes" / "A.md").resolve()


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


def test_allowed_roots_include_staging_and_vault(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    vault = tmp_path / "vault"
    monkeypatch.setenv("ZR_STAGING", str(staging))
    monkeypatch.setenv("VAULT", str(vault))

    roots = set(map(os.fspath, deepseek_agent.allowed_roots()))

    assert os.fspath(staging.resolve()) in roots
    assert os.fspath(vault.resolve()) in roots
