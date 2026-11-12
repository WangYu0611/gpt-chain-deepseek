from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .demo import run_demo
from .dsh import DshRunner
from .policy import PolicyError, canonical_digest, require_live
from .state import StateStore
from .worker import Worker, load_config, plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline-first Harness laboratory; no automatic external sending")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        sub = subs.add_parser(name)
        sub.add_argument("--config", type=Path, default=Path("config.example.json"))
        sub.add_argument("--event", type=Path, required=True)
        if name == "run":
            sub.add_argument("--execute", action="store_true")
    sub = subs.add_parser("demo")
    sub.add_argument("--output", type=Path, default=Path(".runtime/demo"))
    sub.add_argument("--scenario", choices=["all", *"ABCDEF"], default="all")
    sub = subs.add_parser("doctor")
    sub.add_argument("--config", type=Path, default=Path("config.example.json"))
    for name in ("status", "pause", "resume", "export-state"):
        sub = subs.add_parser(name)
        sub.add_argument("--state", type=Path, default=Path(".runtime/state.sqlite3"))
    sub = subs.add_parser("publish")
    sub.add_argument("--config", type=Path, default=Path("config.example.json"))
    sub.add_argument("--outbox", type=Path, required=True)
    sub.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            result = run_demo(args.output, args.scenario)
        elif args.command in {"status", "pause", "resume", "export-state"}:
            store = StateStore(args.state)
            if args.command in {"pause", "resume"}:
                getattr(store, args.command)()
            result = store.export_snapshot() if args.command == "export-state" else {"paused": store.paused, "tasks": store.list_tasks()}
        else:
            config = load_config(args.config.resolve())
            if args.command == "doctor":
                result = {"python": sys.version.split()[0], "git": shutil.which("git"), "source_repository_exists": Path(config["source_repository"]).is_dir(), "dsh": DshRunner(config.get("dsh", {})).doctor(), "live_enabled": config.get("live_enabled", False), "network_calls": 0, "blockers": ["No real sending authorized", "Authenticated GitHub controller and separate publisher are not commissioned", "Provider hard quota and operating-system isolation need verification", "ChatGPT web account write permissions and event delivery need live verification"]}
            elif args.command == "publish":
                outbox = json.loads(args.outbox.read_text(encoding="utf-8"))
                payload = {k: v for k, v in outbox.items() if k != "digest"}
                if canonical_digest(payload) != outbox.get("digest"):
                    raise PolicyError("outbox digest mismatch")
                if args.execute:
                    require_live(config, "publish-github")
                    raise PolicyError("BLOCKED: trusted publisher bridge is not commissioned; offline-demo outboxes must never be published")
                result = {"mode": "dry-run", "sent": False, "outbox_mode": outbox["mode"], "repository": outbox["repository"], "issue": outbox["issue"], "proposed_actions": outbox["actions"], "note": "Digest detects corruption, not authenticity. No remote mutation adapter is active."}
            else:
                event = json.loads(args.event.read_text(encoding="utf-8-sig"))
                result = Worker(config).run(event) if args.command == "run" and args.execute else plan(config, event)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PolicyError, ValueError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc), "sent": False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
