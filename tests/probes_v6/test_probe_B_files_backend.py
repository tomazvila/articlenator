"""Reviewer B probes for the round-13 files backend. Each test asserts the CORRECT
behavior: a failing test proves a defect. Tests named test_ok_* show a case that is
not possible (they pass). No network, no LLM."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from tests.unit.test_run_integrity import ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5, _Single, reply

import synth_call as sc  # noqa: E402


def _h1(text: str, title: str) -> str:
    return re.sub(r"(?m)^# .+$", f"# {title}", text, count=1)


class Probes(_Single):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_LLM_BACKEND"] = "files"
        self.xdir = self.staging / "exchange"

    # ------------------------------------------------------------------ B4 / B1
    def test_cached_writer_call_loses_its_worker(self) -> None:
        """A writer reply consumed in pass N is read from the synthesis cache in pass N+1:
        the cached call record has no `worker`, so writer_workers() misses the writer and
        the writer may review its own note."""
        self.fake()
        self.synth()  # pass 1: synth request
        first = reply([("C1", "01:16", "a"), ("C2", "01:22", "b")], [(self.a, ["C1"], self.ta)])
        work(self.xdir, lambda r: first, worker="w1")
        u2 = self.new_unit("synth", [V5])
        self.assertEqual(sc.synth_unit(self.staging, self.zk, u2, [V5])["outcome"], "waiting-for-reply")  # coverage
        work(self.xdir, lambda r: "=====SKIPPED=====\nC2 | not a transferable training claim: only a story\n",
             worker="w2")
        u3 = self.new_unit("synth", [V5])
        res = sc.synth_unit(self.staging, self.zk, u3, [V5])
        self.assertEqual(res["notes"], [self.a])
        rec = json.loads((u3 / "calls" / "synth-p1-0.json").read_text())
        self.assertIn("w1", sc.writer_workers(u3),
                      f"the writer w1 of note A is not in the refuse list; cached record: {rec}")

    # ------------------------------------------------------------------ B2
    def test_second_title_fix_gets_the_first_title_fix_reply(self) -> None:
        """Title-fix call names count calls/titlefix-*.request.json; a cached title fix
        writes no request.json, so in the next pass the second title job gets the name
        titlefix-0 and the CACHED reply of the first job. Note B is lost."""
        self.fake()
        self.synth()
        bad = reply([("C1", "01:16", "a"), ("C2", "01:22", "b")],
                    [("Home", ["C1"], _h1(self.ta, "Home")), ("00 Maps", ["C2"], _h1(self.tb, "00 Maps"))])
        work(self.xdir, lambda r: bad, worker="w1")

        def fixes(r: dict) -> str | None:
            if r["kind"] != "titlefix":
                return None
            if "# Home" in r["user"]:
                return f"=====NOTE: {self.a}=====\n{self.ta}"
            return f"=====NOTE: {self.b}=====\n{self.tb}"
        res = None
        for _ in range(4):
            u = self.new_unit("synth", [V5])
            res = sc.synth_unit(self.staging, self.zk, u, [V5])
            if res["outcome"] != "waiting-for-reply":
                break
            work(self.xdir, fixes, worker="w2")
        self.assertEqual(res["outcome"], "notes")
        tf = [json.loads(p.read_text()) for p in (self.xdir / "requests").glob("*.json")]
        tf = [r for r in tf if r["kind"] == "titlefix"]
        # both title-fix requests went out and both have a reply: the loss is not a missing reply
        self.assertEqual(len(tf), 2)
        self.assertTrue(all(Path(r["reply_path"]).is_file() for r in tf))
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]),
                         f"refused: {res.get('refused')}; problems: {[p['type'] for p in res['problems']]}")

    # ------------------------------------------------------------------ B4
    def test_meta_worker_list_crashes_every_later_review(self) -> None:
        """meta `worker` is not checked for type: a list is accepted for a writer call and
        writer_workers() then raises TypeError (unhashable) for every review of the unit."""
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u1"}]
        s = {"model": "m", "max_tokens": 10}
        with self.assertRaises(sc.PendingReply) as cm:
            sc.files_call(self.unit, "synth-p1-0", msgs, s)
        key = cm.exception.key
        (self.xdir / "replies" / f"{key}.txt").write_text("x")
        (self.xdir / "replies" / f"{key}.meta.json").write_text(json.dumps({"key": key, "worker": ["w1"]}))
        sc.files_call(self.unit, "synth-p1-0", msgs, s)  # accepted
        try:
            sc.writer_workers(self.unit)
        except TypeError as exc:
            self.fail(f"writer_workers crashed: {exc!r} (a meta `worker` list was accepted)")

    def test_meta_worker_int_and_str_crash_sorted(self) -> None:
        s = {"model": "m", "max_tokens": 10}
        for i, w in enumerate((7, "w1")):
            msgs = [{"role": "user", "content": f"u{i}"}]
            with self.assertRaises(sc.PendingReply) as cm:
                sc.files_call(self.unit, f"synth-p{i}-0", msgs, s)
            key = cm.exception.key
            (self.xdir / "replies" / f"{key}.txt").write_text("x")
            (self.xdir / "replies" / f"{key}.meta.json").write_text(json.dumps({"key": key, "worker": w}))
            sc.files_call(self.unit, f"synth-p{i}-0", msgs, s)
        try:
            sc.writer_workers(self.unit)
        except TypeError as exc:
            self.fail(f"writer_workers crashed: {exc!r}")

    def test_meta_json_array_crashes_files_call(self) -> None:
        """A meta file that is valid JSON but not an object raises AttributeError, which
        no caller catches (the unit crashes instead of a refusal)."""
        msgs = [{"role": "user", "content": "u"}]
        s = {"model": "m", "max_tokens": 10}
        with self.assertRaises(sc.PendingReply) as cm:
            sc.files_call(self.unit, "synth-p1-0", msgs, s)
        key = cm.exception.key
        (self.xdir / "replies" / f"{key}.txt").write_text("x")
        (self.xdir / "replies" / f"{key}.meta.json").write_text("[]")
        try:
            sc.files_call(self.unit, "synth-p1-0", msgs, s)
        except (sc.PendingReply, ri.HarnessError):
            pass
        except AttributeError as exc:
            self.fail(f"files_call crashed on a JSON-array meta: {exc!r}")

    # ------------------------------------------------------------------ B5
    def test_refusal_is_not_visible_in_exchange_list(self) -> None:
        """After a same-worker refusal the request is listed again (good), but the row says
        nothing about the refusal and `must_not_share_worker_with` holds request KEYS, not
        worker ids: a dispatcher that reads the list can send the same worker again."""
        self.fake()
        self.synth()
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="writer-1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        import review_call as rc
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]), worker="writer-1")
        rc.review_unit(self.staging, self.zk, u)
        rows = [r for r in sc.exchange_list(self.xdir) if r["kind"] == "review"]
        self.assertEqual(len(rows), 1)  # listed again: not stuck
        row = rows[0]
        self.assertTrue(list((self.xdir / "replies").glob("*.refused-same-worker.txt")))
        shown = json.dumps(row)
        self.assertTrue("refused" in shown or "writer-1" in shown,
                        f"the open row gives no refusal and no worker id: {row}")

    # ------------------------------------------------------------------ B1
    def test_waiting_review_uses_up_pending_cycles(self) -> None:
        """Each pass in which a pending note's review reply is missing counts a pending
        cycle. After ZR_MAX_PENDING_CYCLES (5) passes the folder is left for the operator,
        and the reply that arrives later is never consumed."""
        import review_call as rc
        self.fake()
        self.synth()
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="w1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        for _ in range(6):
            units = ri.pending_units(self.staging, self.run_dir, os.getpid())
            if not units:
                break
            for pu in units:
                rc.review_unit(self.staging, self.zk, Path(pu))
                ri.finalize(self.staging, self.run_dir, Path(pu), [], 0, self.zk)
        self.assertGreater(sc.exchange_status(self.xdir)["open"], 0)
        work(self.xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker="w2")
        self.assertTrue(ri.pending_units(self.staging, self.run_dir, os.getpid()),
                        "the answered review is never consumed: the pending folder waited 5 cycles for a reply")

    # ------------------------------------------------------------------ B1
    def test_invalid_review_reply_is_permanent(self) -> None:
        """A review reply that does not validate is re-read in every later pass (the key is
        the same), so a pending re-review can never get a new answer."""
        import review_call as rc
        os.environ["ZR_REVIEW_RETRIES"] = "0"
        self.fake()
        self.synth()
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="w1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: "not json" if r["kind"] == "review" else None, worker="w2")
        ends = []
        for _ in range(3):
            ends += [r["end"] for r in rc.review_unit(self.staging, self.zk, u, only_new=False)]
        n_req = len(list((self.xdir / "requests").glob("*.json")))
        self.assertIn("waiting-for-reply", ends,
                      f"every re-review re-reads the same invalid reply: ends {ends}, requests {n_req}")

    # ------------------------------------------------------------------ cases that are not possible
    def test_ok_refused_request_is_listed_again(self) -> None:
        self.fake()
        self.synth()
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="w1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        import review_call as rc
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]), worker="w1")
        self.assertEqual(rc.review_unit(self.staging, self.zk, u)[0]["end"], "waiting-for-reply")
        self.assertEqual(sc.exchange_status(self.xdir)["open"], 1)

    def test_ok_reply_survives_a_crash_after_consume(self) -> None:
        """Replies are never deleted on consume: a crash after files_call and before the
        cache write re-reads the reply in the next pass."""
        msgs = [{"role": "user", "content": "u"}]
        s = {"model": "m", "max_tokens": 10}
        with self.assertRaises(sc.PendingReply) as cm:
            sc.files_call(self.unit, "synth-p1-0", msgs, s)
        key = cm.exception.key
        (self.xdir / "replies" / f"{key}.txt").write_text("hello\n")
        self.assertEqual(sc.files_call(self.unit, "synth-p1-0", msgs, s)[0], "hello\n")
        self.assertEqual(sc.files_call(self.unit, "synth-p1-0", msgs, s)[0], "hello\n")

    def test_ok_exchange_dir_outside_staging_is_refused(self) -> None:
        os.environ["ZR_EXCHANGE_DIR"] = "../../outside"
        try:
            with self.assertRaises(ri.HarnessError):
                sc.exchange_dir(self.staging)
            os.environ["ZR_EXCHANGE_DIR"] = "/tmp"
            with self.assertRaises(ri.HarnessError):
                sc.exchange_dir(self.staging)
        finally:
            os.environ.pop("ZR_EXCHANGE_DIR", None)

    def test_ok_traversal_title_is_not_written(self) -> None:
        self.fake()
        self.synth()
        evil = _h1(self.ta, "../../../escape")
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [("../../../escape", ["C1"], evil)])
             if r["kind"] == "synth" else None, worker="w1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        self.assertFalse(list(self.tmp.rglob("escape.md")))
