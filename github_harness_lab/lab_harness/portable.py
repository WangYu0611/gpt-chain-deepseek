"""CLI for a shared, profile-driven bounded-document engine.

inspect/profile/bundle are local only. generate/publish require an explicit
--execute, an immutable remote owner arm and a matching deployed workflow.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .portable_policy import PUBLIC_BINDING, strict_json, validate_activation
from .project_profile import load_profile, make_profile, profile_digest, repository_namespace


def read_object(path, limit=65536):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Linked input files are not accepted")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Input exceeds its byte limit")
    result = strict_json(raw.decode("utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("Input must be a JSON object")
    return result


def _identity(profile, activation, root, env):
    from .portable_bundle import controller_digest
    validate_activation(profile, activation)
    if (env.get("EXPECTED_PROFILE_SHA256") != profile_digest(profile)
            or env.get("EXPECTED_CONTROLLER_SHA256") != activation["controller_sha256"]
            or controller_digest(root) != activation["controller_sha256"]):
        raise ValueError("Profile or installed controller differs from the reviewed workflow")


def _local_provider(activation):
    """Only the separately reviewed official desktop installation is supported."""
    from .bounded_dsh import validate_config
    dsh = activation.get("dsh")
    if not isinstance(dsh, dict):
        raise ValueError("Official desktop provider configuration is required")
    validate_config(dsh)
    install = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / "DeepSeek Harness"
    credential = Path.home() / ".dsh" / ".credentials.yaml"
    if (not isinstance(dsh.get("desktop_install"), str) or not isinstance(dsh.get("credentials_path"), str)
            or Path(dsh["desktop_install"]).resolve() != install.resolve()
            or Path(dsh["credentials_path"]).resolve() != credential.resolve()
            or not (install / "DeepSeek Harness.exe").is_file()
            or not (install / "resources" / "app.asar").is_file()
            or not credential.is_file()):
        raise ValueError("Use the verified official desktop installation and its login file")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared profile-driven Harness; no live action without --execute")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "bundle", "generate", "publish"):
        child = commands.add_parser(name)
        child.add_argument("--profile", required=True, type=Path)
        if name == "bundle":
            child.add_argument("--output", required=True, type=Path)
        if name == "generate":
            child.add_argument("--activation", required=True, type=Path)
            child.add_argument("--event", required=True, type=Path)
        if name == "publish":
            child.add_argument("--artifact", required=True, type=Path)
            child.add_argument("--journal-root", type=Path)
        if name in {"generate", "publish"}:
            child.add_argument("--execute", action="store_true")
    child = commands.add_parser("profile")
    child.add_argument("--from-config", required=True, type=Path)
    child.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    if args.command == "profile":
        from .repository_setup import load_settings
        # Read exactly the named normal configuration, rather than an arbitrary
        # private activation. This command never creates activation records.
        if args.from_config.name != "config.local.json":
            raise ValueError("--from-config must point to config.local.json")
        if not args.from_config.is_file():
            raise ValueError("Selected configuration file does not exist")
        settings = load_settings(args.from_config.resolve().parent)
        profile = make_profile(settings.repository, settings.default_branch, settings.trusted_actor,
                               source_repository=settings.source_repository)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(profile, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        result = {"status": "PROFILE_SAVED", "path": str(args.output.resolve()), "live_enabled": False}
    else:
        profile = load_profile(args.profile)
        if args.command == "inspect":
            result = {"repository": profile["repository"], "profile_sha256": profile_digest(profile),
                      "namespace": repository_namespace(profile["repository"]),
                      "capability": profile["capability"], "live_enabled": False,
                      "executable_tasks": [spec["task_id"] for spec in profile["document_tasks"]],
                      "stop_task": profile["stop_task"], "network_calls": 0, "model_calls": 0,
                      "coding_capability": "NOT_IMPLEMENTED"}
        elif args.command == "bundle":
            from .portable_bundle import build_bundle
            output = build_bundle(profile, args.output, source_root=root)
            result = {"status": "LOCAL_BUNDLE_READY", "path": str(output), "deployed": False}
        elif not args.execute:
            result = {"status": "DRY_RUN", "repository": profile["repository"], "operation": args.command,
                      "network_calls": 0, "model_calls": 0, "sent": False,
                      "note": "A project profile never authorizes execution."}
        else:
            from .github import GitHubClient
            env = dict(os.environ)
            if args.command == "generate":
                activation = read_object(args.activation)
                event = read_object(args.event, 1024 * 1024)
            else:
                artifact = read_object(args.artifact)
                activation = {"schema": "harness-activation/v1", "enabled": True,
                              **{key: artifact[key] for key in PUBLIC_BINDING}}
            _identity(profile, activation, root, env)
            if args.command == "generate":
                _local_provider(activation)

            def gate(operation):
                validate_activation(profile, activation)
                return operation == "github-read" or args.command == "publish"

            client = GitHubClient(profile["repository"], network_enabled=True,
                                  token_env="GH_HARNESS_TOKEN", authorization_gate=gate)
            if args.command == "generate":
                from .bounded_dsh import run
                from .portable_engine import generate_task
                artifact, output = generate_task(profile, activation, event, env, root=root,
                                                 client=client, generate=run)
                if env.get("GITHUB_OUTPUT"):
                    if "\n" in str(output) or "\r" in str(output):
                        raise ValueError("Invalid artifact output path")
                    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
                        stream.write("artifact_path=" + str(output.resolve()) + "\n")
                result = {"status": "GENERATED", "task_id": artifact["task_id"],
                          "session_id": artifact["session_id"], "model_requests": artifact["model_requests"]}
            else:
                from .portable_publisher import publish_task
                journal_root = args.journal_root or root / ".runtime" / "publisher"
                result = publish_task(client, profile, activation, artifact, env, journal_root=journal_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Never print provider output or tokens. Detailed evidence remains in the
        # bounded local run directory and must be reviewed before sharing.
        print(json.dumps({"status": "BLOCKED", "reason": type(error).__name__}), file=sys.stderr)
        raise SystemExit(2)
