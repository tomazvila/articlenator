"""Round 21 (W6): a speaker-unresolved video end to end through loop.sh with fake workers
(files backend): inventory with a SPEAKERS block -> batch notes -> unresolve_note ->
checker -> review -> publish. The test reads the PUBLISHED vault files, not function output.
No network, no LLM."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.probes_v6 import test_probe_B_loop as _bl
from tests.unit.test_run_integrity import ZR
from tests.unit.test_run_integrity_r13 import review_answer, work

NC = ZR.parent / "tests" / "fixtures" / "note_contract"
EX2 = "vid-EXAMPLE0002"
SPK2 = ("=====SPEAKERS=====\nS1 | Tomas Brink | host | \"welcome back\" 00:00\n"
        "S2 | Mara Kell | guest | \"tomas asked me\" 01:40\n")


class UnresolvedVideoPublishes(unittest.TestCase):
    def test_unresolved_notes_publish_with_live_sibling_links(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="r21-w6-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stg, zk, env, _replies = _bl.LoopProbes._setup(self, tmp)
        q = json.loads((stg / "queue.json").read_text())
        q["items"] = [it for it in q["items"] if it["id"] == EX2]
        (stg / "queue.json").write_text(json.dumps(q))
        env.update(ZR_SYNTH_STAGES="two", MAX_ITERS="20")
        inv = (NC / "inventory_reply_fictional.txt").read_text().split("=====SPEAKERS=====")[0] + SPK2
        batch = (NC / "batch_reply_fictional.txt").read_text()
        sent = {"batch": 0}

        def ans(r: dict) -> str | None:
            if r["kind"] == "inv":
                return inv
            if r["kind"] == "batch":
                sent["batch"] += 1
                return batch if sent["batch"] == 1 else ("=====SKIPPED=====\nC8 | not a transferable training "
                                                         "claim: no content beyond the word warm-up\n")
            if r["kind"] == "review":
                return review_answer(r["user"])
            return None
        codes = []
        for _ in range(8):
            r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            work(stg / "exchange", ans, worker=lambda req: "reviewer-2" if req["kind"] == "review" else "writer-1")
        notes = {p.stem: p.read_text() for p in zk.rglob("*.md")}
        unres = [t for t in notes if t.startswith("Unresolved Speaker In ")]
        n_batch = len(re.findall(r"(?m)^=====NOTE: ", batch))
        # every note of the video publishes (W6: a scrubbed sibling link made all of them dead-link)
        self.assertEqual(len(unres), n_batch, (codes, sorted(notes), r.stdout[-1500:], r.stderr[-1500:]))
        # the sibling links of the batch reply stay sibling links (renamed with their target),
        # never scrubbed into dead links that are then removed
        sib = sum(1 for t in unres for link in re.findall(r"\[\[([^\]|#]+)", notes[t]) if link in unres)
        self.assertEqual(sib, len(re.findall(r"\[\[Tomas Brink ", batch)), {t: re.findall(r"\[\[[^\]]+\]\]", notes[t]) for t in unres})
        for t in unres:
            text = notes[t]
            self.assertNotIn("[[the speaker", text)
            self.assertIn("speaker_status: unresolved", text)
            for link in re.findall(r"\[\[([^\]|#]+)", text):
                self.assertIn(link.strip(), notes, f"{t}: dead link [[{link}]]")
