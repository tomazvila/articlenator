from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QUEUE_CLAIM = ROOT / "zettel_ralph" / "queue_claim.py"


def write_queue(staging: Path, items: list[dict]) -> None:
    staging.mkdir()
    (staging / "queue.json").write_text(
        json.dumps({"version": 1, "items": items}, indent=2),
        encoding="utf-8",
    )


def run_queue_claim(staging: Path, *args: str) -> str:
    env = os.environ.copy()
    env["ZR_STAGING"] = str(staging)
    proc = subprocess.run(
        [sys.executable, str(QUEUE_CLAIM), *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout.strip()


def read_items(staging: Path) -> list[dict]:
    return json.loads((staging / "queue.json").read_text(encoding="utf-8"))["items"]


def test_reclaim_only_releases_stale_claim_without_claiming_next_unit(tmp_path):
    staging = tmp_path / "staging"
    write_queue(
        staging,
        [
            {
                "id": "a",
                "kind": "article",
                "stage": "claimed",
                "worker": 0,
                "lit_note": "staging/lit/a.md",
            },
            {
                "id": "b",
                "kind": "article",
                "stage": "extracted",
                "lit_note": "staging/lit/b.md",
            },
        ],
    )

    output = run_queue_claim(staging, "--worker", "0", "--of", "1", "--reclaim")

    assert output == "{}"
    items = read_items(staging)
    assert items[0]["stage"] == "extracted"
    assert "worker" not in items[0]
    assert items[1]["stage"] == "extracted"
    assert "worker" not in items[1]


def test_claim_after_reclaim_can_process_released_unit(tmp_path):
    staging = tmp_path / "staging"
    write_queue(
        staging,
        [
            {
                "id": "a",
                "kind": "article",
                "stage": "claimed",
                "worker": 0,
                "lit_note": "staging/lit/a.md",
            }
        ],
    )

    run_queue_claim(staging, "--worker", "0", "--of", "1", "--reclaim")
    output = run_queue_claim(staging, "--worker", "0", "--of", "1")

    assert json.loads(output)["ids"] == ["a"]
    assert read_items(staging)[0]["stage"] == "claimed"
