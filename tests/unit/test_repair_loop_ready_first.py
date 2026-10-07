"""Shell-flow regression for ready-first single repairs and legacy agent ordering."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER = REPO_ROOT / "zettel_ralph" / "repair_loop.sh"


class RepairLoopReadyFirst(unittest.TestCase):
    def run_driver(self, mode: str, repair_rc: int = 0) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory(prefix="ready-first-") as temp:
            root = Path(temp)
            app = root / "driver"
            app.mkdir()
            staging = root / "staging"
            staging.mkdir()
            (staging / "known_sources.json").write_text("{}")
            vault = root / "vault"
            vault.mkdir()
            bindir = root / "bin"
            bindir.mkdir()
            shim = bindir / "python3"
            shim.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  */synth_call.py) exit 0 ;;\n"
                "  */run_integrity.py)\n"
                "    if [ \"$2\" = repair-plan ]; then\n"
                "      printf '%s\\n' '{\"state\":\"open\",\"sources\":[\"mock-source\"],\"mark_unsupported\":false}'\n"
                "      exit 0\n"
                "    fi ;;\n"
                "esac\n"
                f"exec {sys.executable} \"$@\"\n"
            )
            shim.chmod(0o755)
            (app / "repair_loop.sh").write_bytes(DRIVER.read_bytes())
            (app / "run_lib.sh").write_text(
                "zr_run_start() { RUN_ID=mock; RUN_DIR=$STAGING/run; echo start >>\"$FLOW_LOG\"; }\n"
                "zr_run_summary() { echo summary >>\"$FLOW_LOG\"; }\n"
                "zr_pending_reviews() { echo pending >>\"$FLOW_LOG\"; }\n"
                "zr_repair_single() { echo repair >>\"$FLOW_LOG\"; return \"$TEST_REPAIR_RC\"; }\n"
                "zr_budget_check() { echo budget >>\"$FLOW_LOG\"; return 0; }\n"
                "zr_new_unit() { echo new_unit >>\"$FLOW_LOG\"; mkdir -p \"$STAGING/mock-unit\"; echo \"$STAGING/mock-unit\"; }\n"
                "zr_agent() { echo agent >>\"$FLOW_LOG\"; return 0; }\n"
                "zr_fix_turns() { echo fix_turns >>\"$FLOW_LOG\"; return 0; }\n"
                "zr_source_check() { echo source_check >>\"$FLOW_LOG\"; return 0; }\n"
                "zr_review_repair() { echo review_repair >>\"$FLOW_LOG\"; return 0; }\n"
                "zr_finalize() { echo finalize >>\"$FLOW_LOG\"; return 0; }\n"
            )
            log = root / "flow.log"
            env = os.environ.copy()
            env.update({
                "PATH": f"{bindir}:/bin:/usr/bin",
                "ZR_STAGING": str(staging),
                "VAULT": str(vault),
                "FLOW_LOG": str(log),
                "TEST_REPAIR_RC": str(repair_rc),
                "DEEPSEEK_API_KEY": "test-no-network",
                "ZR_REPAIR_AGENT_MODE_I_KNOW": "0",
                "ZR_SYNTH_MODE": mode,
            })
            if mode == "agent":
                env["ZR_REPAIR_AGENT_MODE_I_KNOW"] = "1"
            result = subprocess.run(
                ["/bin/bash", str(app / "repair_loop.sh"), "Old note.md"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            return result, log.read_text().splitlines() if log.exists() else []

    def test_single_mode_success_keeps_ready_first_order(self) -> None:
        result, flow = self.run_driver("single", 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(flow, ["start", "repair", "pending", "summary"])

    def test_single_mode_failure_still_runs_pending_and_preserves_repair_status(self) -> None:
        result, flow = self.run_driver("single", 7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(flow, ["start", "repair", "pending", "summary"])

    def test_agent_mode_retains_pending_first_order(self) -> None:
        result, flow = self.run_driver("agent", 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(flow.index("pending"), flow.index("budget"))
        self.assertLess(flow.index("pending"), flow.index("agent"))
        self.assertEqual(flow[-1], "summary")


if __name__ == "__main__":
    unittest.main()
