"""Secret handling for the agent tools and the publish step.

- `safe_env()`: the environment for helper processes, without API keys, tokens,
  secrets, passwords or cookies.
- `scrub()`: replace every secret value in a text (tool results, errors, logs).
- `find_secrets()`: secret values or key-shaped strings in a text; finalize
  quarantines a note that holds one, and the agent's write tools refuse it.
"""
from __future__ import annotations

import os
import re

# Names: *_KEY, *KEY_*, *TOKEN*, *SECRET*, *PASSWORD*, *COOKIE*, *CREDENTIAL*.
SECRET_NAME = re.compile(r"(_KEY$|_KEY_|^KEY$|API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|CREDENTIAL)", re.I)
# Key shapes. The look-behind keeps a word or URL slug such as "how-to-sk-a-question"
# from matching: a key starts at a word boundary that is not "-", "/" or "_".
_B = r"(?<![\w/-])"
KEY_SHAPES = re.compile(
    _B + r"(sk-or-v1-[A-Za-z0-9]{16,}|sk-(?:proj-|ant-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{24,}|"
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,})|-----BEGIN [A-Z ]*PRIVATE KEY-----|auth_token=[A-Za-z0-9]{16,}"
)
# "password: hunter2", "token=abc...": a secret word followed by a value (defense in depth
# for tool results; a note or a transcript should never hold these).
WORD_VALUE = re.compile(
    r"(?i)\b(password|passwd|passphrase|pwd|token|secret|api[_ -]?key|apikey|access[_ -]?key|"
    r"private[_ -]?key|client[_ -]?secret)\b(\s*[:=]\s*)([\"']?)([^\s\"',;\[]{6,})|"
    r"\b(bearer)(\s+)()([A-Za-z0-9._~+/-]{12,})")
MIN_SECRET_LEN = 8
REDACTED = "[redacted]"


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name))


def secret_values(env: dict[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    vals = {v.strip() for k, v in env.items() if is_secret_name(k) and v and len(v.strip()) >= MIN_SECRET_LEN}
    return sorted(vals, key=len, reverse=True)


def safe_env(env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if env is None else env)
    return {k: v for k, v in env.items() if not is_secret_name(k)}


def scrub(text: str) -> str:
    """Remove secret values, key-shaped strings and word-plus-value secrets."""
    if not text:
        return text
    text = KEY_SHAPES.sub(REDACTED, text)
    for v in secret_values():
        text = text.replace(v, REDACTED)
    def _cut(m: re.Match) -> str:
        g = [x for x in m.groups() if x is not None]
        return f"{g[0]}{g[1]}{g[2]}{REDACTED}"
    return WORD_VALUE.sub(_cut, text)


def find_secrets(text: str) -> list[str]:
    """Names of the secrets found (never the values)."""
    out = []
    env = os.environ
    for k, v in env.items():
        if is_secret_name(k) and v and len(v.strip()) >= MIN_SECRET_LEN and v.strip() in text:
            out.append(f"value of ${k}")
    out += [f"key-shaped text '{m.group(0)[:6]}...'" for m in KEY_SHAPES.finditer(text)]
    out += [f"'{m.group(0).split()[0][:12]}' followed by a value" for m in WORD_VALUE.finditer(text)]
    return out
