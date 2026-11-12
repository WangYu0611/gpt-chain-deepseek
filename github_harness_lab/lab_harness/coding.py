"""Explicit CLI for the shared code-change worker. Inspect/export are inert."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .coding_contract import PUBLIC_BINDING, load_profile, profile_digest, validate_activation
from .portable import read_object
from .project_profile import repository_namespace


def _identity(profile, activation, root, env):
    from .coding_bundle import controller_digest
    validate_activation(profile, activation)
    if (env.get("EXPECTED_PROFILE_SHA256") != profile_digest(profile)
            or env.get("EXPECTED_CONTROLLER_SHA256") != activation["controller_sha256"]
            or controller_digest(root) != activation["controller_sha256"]):
        raise ValueError("Installed code/profile differs from the reviewed workflow")


def _local_provider(activation):
    from .coding_dsh import validate_config
    dsh = activation.get("dsh")
    if not isinstance(dsh, dict):
        raise ValueError("Official desktop provider configuration is required")
    validate_config(dsh)
    install = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Programs/DeepSeek Harness"
    credentials = Path.home() / ".dsh/.credentials.yaml"
    if (not isinstance(dsh.get("desktop_install"), str) or not isinstance(dsh.get("credentials_path"), str)
            or Path(dsh["desktop_install"]).resolve() != install.resolve()
            or Path(dsh["credentials_path"]).resolve() != credentials.resolve()
            or not (install / "DeepSeek Harness.exe").is_file()
            or not (install / "resources/app.asar").is_file() or not credentials.is_file()):
        raise ValueError("Use the verified official desktop installation and its login file")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared project code execution with explicit continuous activation")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "bundle", "generate", "publish", "stop", "status"):
        command = commands.add_parser(name)
        command.add_argument("--profile", required=True, type=Path)
        if name == "bundle":
            command.add_argument("--output", required=True, type=Path)
        if name == "generate":
            command.add_argument("--activation", required=True, type=Path)
            command.add_argument("--event", required=True, type=Path)
        if name == "publish":
            command.add_argument("--artifact", required=True, type=Path)
            command.add_argument("--journal-root", type=Path)
        if name in {"generate", "publish"}:
            command.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    root = Path(__file__).resolve().parent.parent
    state = root / ".runtime/coding" / repository_namespace(profile["repository"])
    if args.command == "inspect":
        result = {"status": "LOCAL_PROFILE_ONLY", "repository": profile["repository"],
                  "profile_sha256": profile_digest(profile), "goal": profile["goal"],
                  "limits": profile["limits"], "deployed": False, "network_calls": 0, "model_calls": 0}
    elif args.command == "bundle":
        from .coding_bundle import build_bundle
        output = build_bundle(profile, args.output, source_root=root)
        result = {"status": "LOCAL_CODE_BUNDLE_READY", "path": str(output), "deployed": False}
    elif args.command == "stop":
        from .portable_engine import _folder, _atomic
        state = _folder(root, ".runtime/coding/" + repository_namespace(profile["repository"]))
        _atomic(state / "STOP", {"repository": profile["repository"], "reason": "Operator requested local stop"})
        result = {"status": "LOCAL_GENERATION_STOPPED", "path": str(state / "STOP"),
                  "note": "Close the control Issue and pause the web tasks to revoke cloud publication and dispatch too."}
    elif args.command == "status":
        result = {"repository": profile["repository"], "local_stop": (state / "STOP").exists(),
                  "lease_present": (state / "lease.json").exists(), "remote_status_checked": False,
                  "claims": len(list(state.glob("*/*/claim.json"))) if state.is_dir() else 0}
    elif not args.execute:
        result = {"status": "DRY_RUN", "operation": args.command, "repository": profile["repository"],
                  "network_calls": 0, "model_calls": 0, "sent": False}
    else:
        from .github import GitHubClient
        env = dict(os.environ)
        if args.command == "generate":
            activation = read_object(args.activation)
            event = read_object(args.event, 1024 * 1024)
        else:
            artifact = read_object(args.artifact, 24 * 1024 * 1024)
            activation = {"schema": "harness-code-activation/v1", "enabled": True,
                          **{key: artifact[key] for key in PUBLIC_BINDING}}
        _identity(profile, activation, root, env)
        if args.command == "generate":
            if not str(root).isascii():
                raise ValueError("Install the trusted controller in its dedicated ASCII directory before execution")
            _local_provider(activation)

        def gate(operation):
            validate_activation(profile, activation)
            if args.command == "generate":
                if (state / "STOP").exists() or read_object(args.activation) != activation:
                    raise ValueError("Local activation changed or was stopped")
                return operation == "github-read"
            return True

        client = GitHubClient(profile["repository"], network_enabled=True, token_env="GH_HARNESS_TOKEN",
                              authorization_gate=gate, max_response_bytes=48 * 1024 * 1024)
        if args.command == "generate":
            from .coding_dsh import run
            from .coding_engine import generate_task
            artifact, output = generate_task(profile, activation, event, env, root=root, client=client, generate=run)
            if env.get("GITHUB_OUTPUT"):
                if "\n" in str(output) or "\r" in str(output):
                    raise ValueError("Invalid artifact output path")
                with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
                    stream.write("artifact_path=" + str(output.resolve()) + "\n")
            result = {"status": "GENERATED", "task_id": artifact["task"]["task_id"],
                      "number": artifact["task"]["number"], "check_status": artifact["check_status"],
                      "session_id": artifact["session_id"], "model_requests": 1}
        else:
            from .coding_publisher import publish_task
            result = publish_task(client, profile, activation, artifact, env,
                                  journal_root=args.journal_root or root / ".runtime/code-publisher")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "BLOCKED", "reason": type(error).__name__}), file=sys.stderr)
        raise SystemExit(2)
