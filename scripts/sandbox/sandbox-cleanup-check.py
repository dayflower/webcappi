#!/usr/bin/env python3
"""Inject setup failure and SIGTERM into real owned sandbox-policy clusters."""

import json
from pathlib import Path
import runpy
import signal
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SMOKE = runpy.run_path(str(SCRIPTS / "helm-smoke.py"))


def main():
    args = SMOKE["parse_args"]()
    if args.sandbox != "enabled":
        raise SystemExit("cleanup qualification requires sandbox enabled")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    handlers = {
        sig: signal.signal(sig, SMOKE["interrupted"])
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for case in ("setup-failure", "sigterm"):
            with tempfile.TemporaryDirectory(
                prefix="webcappi-sandbox-cleanup-"
            ) as directory:
                cluster = SMOKE["Cluster"](args, Path(directory))
                install = cluster.policy.install

                def fail_after_load():
                    install()
                    raise RuntimeError("injected failure after policy load")

                if case == "setup-failure":
                    cluster.policy.install = fail_after_load
                injected = False
                try:
                    with cluster:
                        injected = True
                        signal.raise_signal(signal.SIGTERM)
                except RuntimeError as error:
                    if (
                        case != "setup-failure"
                        or str(error) != "injected failure after policy load"
                    ):
                        raise
                except KeyboardInterrupt:
                    if case != "sigterm" or not injected:
                        raise
                else:
                    raise AssertionError("failure injection did not interrupt setup")
                if cluster.policy.apparmor:
                    assert cluster.policy.record["apparmor_removed"] is True
                assert (
                    SMOKE["run"]("docker", "inspect", cluster.policy.node, check=False)
                    is None
                )
                report = {
                    "result": "passed",
                    "case": case,
                    "policy": cluster.policy.record,
                    "node_removed": True,
                }
                (args.artifacts / (case + ".json")).write_text(
                    json.dumps(report, indent=2) + "\n"
                )
                print(f"PASS: {case} removed owned node and sandbox policy", flush=True)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
