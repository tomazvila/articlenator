#!/usr/bin/env python3
"""Tiny DeepSeek-backed tool runner for the Ralph loops.

The Ralph harness needs an agent that can read a bounded set of files, edit notes, and run
the existing Python helper scripts. This script provides exactly those tools over the
OpenAI-compatible DeepSeek chat completions API.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
HELPERS = {
    "index_query.py",
    "index_add.py",
    "index_rebuild.py",
    "queue_mark.py",
    "queue_claim.py",
    "review_queue.py",
    "validate.py",
}
ENV_ALLOWLIST = {
    "AGENTS_FILE",
    "HERE",
    "NWORKERS",
    "QUEUE",
    "STAGING",
    "VAULT",
    "WORKER",
    "ZK_DIR",
    "ZK_FOLDER",
    "ZR_STAGING",
}


class AgentError(RuntimeError):
    """Raised for model or tool errors that should stop the current iteration."""


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def load_api_key() -> str:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"].strip()

    key_file = Path(os.environ.get("DEEPSEEK_API_KEY_FILE", REPO / "deepseek.txt"))
    if not key_file.is_file():
        raise AgentError(
            "DeepSeek API key not found. Set DEEPSEEK_API_KEY or "
            f"DEEPSEEK_API_KEY_FILE (looked for {key_file})."
        )
    key = key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise AgentError(f"DeepSeek API key file is empty: {key_file}")
    return key


def allowed_roots() -> list[Path]:
    roots = [HERE, Path(os.environ.get("ZR_STAGING") or HERE / "staging")]
    for name in ("VAULT", "ZK_DIR"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    return [p.expanduser().resolve() for p in roots]


def resolve_allowed_path(raw_path: str) -> Path:
    if not raw_path:
        raise AgentError("Path must not be empty.")

    candidate = Path(os.path.expandvars(raw_path)).expanduser()
    if not candidate.is_absolute():
        candidate = HERE / candidate
    resolved = candidate.resolve()

    for root in allowed_roots():
        if resolved == root or root in resolved.parents:
            return resolved

    roots = ", ".join(str(p) for p in allowed_roots())
    raise AgentError(f"Path is outside allowed roots: {resolved}. Allowed roots: {roots}")


def compact_output(text: str, limit: int | None = None) -> str:
    max_chars = limit or int(os.environ.get("DEEPSEEK_TOOL_OUTPUT_CHARS", "30000"))
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...[tool output truncated]...\n" + text[-half:]


def read_file(path: str, start_line: int = 1, line_count: int = 400) -> dict[str, Any]:
    target = resolve_allowed_path(path)
    if not target.is_file():
        raise AgentError(f"File does not exist: {target}")
    if start_line < 1:
        raise AgentError("start_line must be >= 1")
    if line_count < 1:
        raise AgentError("line_count must be >= 1")

    max_lines = int(os.environ.get("DEEPSEEK_READ_MAX_LINES", "20000"))
    line_count = min(line_count, max_lines)
    lines = target.read_text(encoding="utf-8").splitlines()
    start_index = start_line - 1
    selected = lines[start_index : start_index + line_count]
    body = "\n".join(f"{start_index + i + 1}: {line}" for i, line in enumerate(selected))
    return {
        "path": str(target),
        "start_line": start_line,
        "line_count": len(selected),
        "total_lines": len(lines),
        "content": compact_output(body, int(os.environ.get("DEEPSEEK_READ_MAX_CHARS", "220000"))),
    }


def list_dir(path: str = ".", max_entries: int = 200) -> dict[str, Any]:
    target = resolve_allowed_path(path)
    if not target.is_dir():
        raise AgentError(f"Directory does not exist: {target}")
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    max_entries = max(1, min(max_entries, 1000))
    return {
        "path": str(target),
        "entries": [
            {"name": p.name, "type": "dir" if p.is_dir() else "file"} for p in entries[:max_entries]
        ],
        "truncated": len(entries) > max_entries,
    }


def find_files(root: str, pattern: str, max_entries: int = 200) -> dict[str, Any]:
    target = resolve_allowed_path(root)
    if not target.is_dir():
        raise AgentError(f"Directory does not exist: {target}")
    if ".." in Path(pattern).parts:
        raise AgentError("Pattern must not traverse upward.")
    max_entries = max(1, min(max_entries, 1000))
    matches = sorted(p for p in target.glob(pattern) if p.is_file())
    return {
        "root": str(target),
        "pattern": pattern,
        "matches": [str(p) for p in matches[:max_entries]],
        "truncated": len(matches) > max_entries,
    }


def write_file(path: str, content: str) -> dict[str, Any]:
    target = resolve_allowed_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    return {"path": str(target), "bytes": len(content.encode("utf-8"))}


def append_file(path: str, content: str) -> dict[str, Any]:
    target = resolve_allowed_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(content)
    return {"path": str(target), "bytes_appended": len(content.encode("utf-8"))}


def replace_in_file(path: str, old: str, new: str, expected_replacements: int = 1) -> dict[str, Any]:
    if not old:
        raise AgentError("old text must not be empty.")
    target = resolve_allowed_path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected_replacements:
        raise AgentError(
            f"Expected {expected_replacements} replacement(s), found {count} in {target}."
        )
    return write_file(str(target), text.replace(old, new, expected_replacements))


def move_file(src: str, dst: str) -> dict[str, Any]:
    source = resolve_allowed_path(src)
    target = resolve_allowed_path(dst)
    if not source.is_file():
        raise AgentError(f"File does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)
    return {"src": str(source), "dst": str(target)}


def helper_path(raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (HERE / candidate).resolve()
    if resolved.parent != HERE or resolved.name not in HELPERS:
        raise AgentError(f"Only Ralph helper scripts may be run, got: {raw}")
    return resolved


def run_command(command: str, args: list[str] | None = None, timeout_seconds: int = 120) -> dict[str, Any]:
    args = args or []
    if command == "pwd":
        if args:
            raise AgentError("pwd does not accept arguments.")
        return {"stdout": str(HERE) + "\n", "stderr": "", "returncode": 0}

    if command not in {"python", "python3"}:
        raise AgentError("Only pwd, python, and python3 commands are allowed.")
    if not args:
        raise AgentError("python command requires a helper script argument.")

    script = helper_path(args[0])
    cmd = [sys.executable, str(script), *[str(a) for a in args[1:]]]
    timeout_seconds = max(1, min(timeout_seconds, int(os.environ.get("DEEPSEEK_TOOL_TIMEOUT", "300"))))
    proc = subprocess.run(
        cmd,
        cwd=HERE,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "command": " ".join(shlex.quote(part) for part in cmd),
        "returncode": proc.returncode,
        "stdout": compact_output(proc.stdout),
        "stderr": compact_output(proc.stderr),
    }


def get_env(name: str) -> dict[str, Any]:
    if name not in ENV_ALLOWLIST:
        raise AgentError(f"Environment variable is not allowlisted: {name}")
    return {"name": name, "value": os.environ.get(name, "")}


def finish(summary: str = "") -> dict[str, Any]:
    return {"finished": True, "summary": summary}


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 file from the Ralph harness, staging directory, or configured vault. Use line ranges for large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "default": 1},
                    "line_count": {"type": "integer", "default": 400},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories in an allowed directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "max_entries": {"type": "integer", "default": 200},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files below an allowed directory using a glob pattern, such as '01 Permanent Notes/*.md'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string"},
                    "pattern": {"type": "string"},
                    "max_entries": {"type": "integer", "default": 200},
                },
                "required": ["root", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Atomically write a UTF-8 file inside the Ralph harness, staging directory, or configured vault.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append UTF-8 content to an allowed file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace exact text in an allowed UTF-8 file. Fails unless the exact expected count is found.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_replacements": {"type": "integer", "default": 1},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file within the allowed roots.",
            "parameters": {
                "type": "object",
                "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run only pwd or python/python3 with a Ralph helper script: index_query.py, index_add.py, queue_mark.py, validate.py, review_queue.py, queue_claim.py, or index_rebuild.py.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "enum": ["pwd", "python", "python3"]},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "default": 120},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_env",
            "description": "Read an allowlisted Ralph environment variable.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call when the single work unit is complete. Include a concise verification summary.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": [],
            },
        },
    },
]

# Transient network faults (SSL EOF, resets) killed whole agent runs. Retry a few
# times with exponential backoff before giving up.
TRANSIENT_ATTEMPTS = max(1, int(os.environ.get("DEEPSEEK_RETRIES", "4")))
TRANSIENT_BACKOFF_CAP_S = 30

TOOL_HANDLERS = {
    "read_file": read_file,
    "list_dir": list_dir,
    "find_files": find_files,
    "write_file": write_file,
    "append_file": append_file,
    "replace_in_file": replace_in_file,
    "move_file": move_file,
    "run_command": run_command,
    "get_env": get_env,
    "finish": finish,
}

SYSTEM_PROMPT = f"""You are the tool-using worker inside the zettel_ralph Ralph harness.

You must complete exactly the one unit described by the user prompt, persist the result to
disk with the available tools, and stop. Read the requested AGENTS file, prompts, template,
state, decisions, and lit note through tools. Never assume unseen files.

Important operating rules:
- Use only the provided tools for filesystem work and command execution.
- Never try to run shell scripts, bash, sh, claude, codex, or nested agents.
- Never open the concept index directly; use index_query.py and index_add.py.
- Mark the unit with queue_mark.py before finishing, unless the user prompt is a review pass.
- Verify by rereading touched notes or running the helper requested by the driver.
- The harness directory is {HERE}.
"""


def chat_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout_seconds: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": False,
        "max_tokens": int(os.environ.get("DEEPSEEK_MAX_TOKENS", "12000")),
    }
    if os.environ.get("DEEPSEEK_TEMPERATURE", "0.2"):
        body["temperature"] = float(os.environ.get("DEEPSEEK_TEMPERATURE", "0.2"))

    endpoint = base_url.rstrip("/") + "/chat/completions"
    last_transient: Exception | None = None
    for attempt in range(TRANSIENT_ATTEMPTS):
        if attempt:
            time.sleep(min(2**attempt, TRANSIENT_BACKOFF_CAP_S))
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AgentError(
                f"DeepSeek API HTTP {exc.code}: {compact_output(detail, 4000)}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            # Transient network fault (SSL EOF, reset, timeout, DNS). One dropped
            # request must not kill a whole agent run; back off and try again.
            last_transient = exc
    raise AgentError(
        f"DeepSeek API connection error after {TRANSIENT_ATTEMPTS} attempts: "
        f"{getattr(last_transient, 'reason', last_transient)}"
    ) from last_transient


def parse_tool_args(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    return json.loads(raw)


def run_tool(call: dict[str, Any]) -> tuple[str, bool]:
    function = call.get("function") or {}
    name = function.get("name", "")
    if name not in TOOL_HANDLERS:
        return _json({"error": f"Unknown tool: {name}"}), False
    try:
        args = parse_tool_args(function.get("arguments"))
        result = TOOL_HANDLERS[name](**args)
        finished = name == "finish" and bool(result.get("finished"))
        return _json({"ok": True, "result": result}), finished
    except Exception as exc:  # noqa: BLE001 - tool errors should go back to the model.
        return _json({"ok": False, "error": str(exc)}), False


def assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        clean["tool_calls"] = message["tool_calls"]
    return clean


def run_agent(prompt: str) -> int:
    api_key = load_api_key()
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("DEEPSEEK_MODEL") or os.environ.get("MODEL") or DEFAULT_MODEL
    max_steps = int(os.environ.get("DEEPSEEK_MAX_STEPS", "80"))
    api_timeout = int(os.environ.get("DEEPSEEK_API_TIMEOUT", "300"))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    for step in range(1, max_steps + 1):
        response = chat_completion(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            timeout_seconds=api_timeout,
        )
        choices = response.get("choices") or []
        if not choices:
            raise AgentError(f"DeepSeek response had no choices: {_json(response)}")

        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if content:
            print(content, flush=True)

        tool_calls = message.get("tool_calls") or []
        finish_reason = choice.get("finish_reason")
        if not tool_calls:
            if finish_reason == "length":
                raise AgentError("DeepSeek response hit max_tokens before finishing.")
            return 0

        messages.append(assistant_message_for_history(message))
        finished = False
        for call in tool_calls:
            tool_result, tool_finished = run_tool(call)
            finished = finished or tool_finished
            if tool_finished:
                try:
                    summary = json.loads(tool_result)["result"].get("summary", "")
                except (KeyError, TypeError, json.JSONDecodeError):
                    summary = ""
                if summary:
                    print(summary, flush=True)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": tool_result,
                }
            )
        if finished:
            return 0

        if step % 10 == 0:
            print(f"[deepseek-agent] completed {step} tool rounds", file=sys.stderr, flush=True)

    raise AgentError(f"DeepSeek agent exceeded DEEPSEEK_MAX_STEPS={max_steps}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", help="Prompt text. If omitted, stdin is used.")
    args = parser.parse_args()
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        print("No prompt supplied.", file=sys.stderr)
        return 2

    started = time.time()
    try:
        rc = run_agent(prompt)
    except AgentError as exc:
        print(f"deepseek_agent error: {exc}", file=sys.stderr)
        return 1
    finally:
        elapsed = time.time() - started
        print(f"[deepseek-agent] elapsed={elapsed:.1f}s", file=sys.stderr, flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
