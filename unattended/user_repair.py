"""Explicit task-scoped user authorization, preserving prior scope and evidence."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import re

from . import gitops, ue_checks
from .storage import read_json, write_json, write_text

MODEL = "claude-opus-5-5"
CHECK_ID = "ue-full-flow-repair"
REQUIRED_ROLES = {"full_flow_request", "report", "retest", "source_manifest", "model_answer"}
OPTIONAL_ROLES = {"source_inventory", "package_short", "operator_summary"}


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _under(path, root):
    if not Path(path).is_absolute():
        raise ValueError("User repair paths must be absolute")
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise ValueError("User repair evidence escaped its authorized directory")
    return ue_checks._safe_under(root, path.relative_to(root))


def load_repair_request(request_path: Path, runtime: Path) -> dict:
    runtime = Path(runtime).absolute()
    path = _under(request_path, runtime / "user-repairs")
    value = ue_checks._object(path, maximum=128 * 1024)
    required = {"version", "id", "task_id", "user_instruction", "authorized_at", "source_head", "source_state_sha256",
                "source_scope_sha256", "source_full_flow_run", "source_session_id", "target_session_id", "model",
                "reasoning_effort", "rework_window", "max_cycles", "evidence"}
    optional = {"plan_path", "plan_sha256"}
    if (not required <= value.keys() or value.keys() - required - optional or
            type(value["version"]) is not int or value["version"] != 1 or
            not re.fullmatch(r"[0-9a-fA-F-]{32,36}", str(value["id"])) or
            path != runtime / "user-repairs" / value["id"] / "request.json" or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", str(value["task_id"])) or
            value["model"] != MODEL or value["reasoning_effort"] != "high"):
        raise ValueError("Invalid explicit user repair schema or model")
    if not isinstance(value["user_instruction"], str) or not value["user_instruction"].strip() or len(value["user_instruction"]) > 32000:
        raise ValueError("An explicit bounded user instruction is required")
    if datetime.fromisoformat(value["authorized_at"]).utcoffset() is None:
        raise ValueError("User authorization time must include its timezone")
    for field, size in (("source_head", 40), ("source_state_sha256", 64), ("source_scope_sha256", 64)):
        if not isinstance(value[field], str) or not re.fullmatch("[0-9a-f]{" + str(size) + "}", value[field]):
            raise ValueError("Invalid user repair source fingerprint")
    for field in ("source_session_id", "target_session_id"):
        if not isinstance(value[field], str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value[field]):
            raise ValueError("Invalid user repair native session")
    for field in ("rework_window", "max_cycles"):
        if type(value[field]) is not int or not 1 <= value[field] <= 3:
            raise ValueError("User repair windows and cycles must be within 1..3")
    run = _under(value["source_full_flow_run"], runtime / "full-flow/runs")
    if run.parent != runtime / "full-flow/runs":
        raise ValueError("User repair must reference one exact full-flow run")
    records = value["evidence"]
    roles = [row.get("role") for row in records if type(row) is dict] if type(records) is list else []
    if (type(records) is not list or len(records) != len(set(roles)) or not REQUIRED_ROLES <= set(roles) or
            set(roles) - REQUIRED_ROLES - OPTIONAL_ROLES):
        raise ValueError("User repair requires all fixed full-flow evidence roles")
    expected = {"full_flow_request": run / "request.json", "report": run / "report.md",
                "retest": run / "checks/retest/receipt.json", "source_manifest": run / "source-manifest.json",
                "package_short": run / "checks/package-short/receipt.json"}
    for row in records:
        if type(row) is not dict or set(row) != {"role", "path", "sha256", "size_bytes"}:
            raise ValueError("Invalid user repair evidence record")
        evidence = _under(row["path"], path.parent if row["role"] in {"source_inventory", "operator_summary"} and
                          Path(row["path"]).absolute().is_relative_to(path.parent) else run)
        if ((row["role"] in expected and evidence != expected[row["role"]]) or
                (row["role"] == "model_answer" and (evidence.name != "answer.json" or not evidence.is_relative_to(run / "native"))) or
                type(row["size_bytes"]) is not int or not evidence.is_file() or evidence.stat().st_size != row["size_bytes"] or
                file_sha(evidence) != row["sha256"]):
            raise ValueError("User repair source evidence changed or belongs to another role")
    full = ue_checks._object(expected["full_flow_request"])
    retest = ue_checks._object(expected["retest"], maximum=32 * 1024 * 1024)
    manifest = ue_checks._object(expected["source_manifest"], maximum=32 * 1024 * 1024)
    if (full.get("task_id") != value["task_id"] or full.get("head_sha") != value["source_head"] or
            full.get("run_dir") != str(run) and Path(full.get("run_dir", "")).absolute() != run or
            retest.get("task_id") != value["task_id"] or retest.get("head_sha") != value["source_head"] or
            retest.get("request_sha256") != full.get("request_sha256") or retest.get("status") not in {"passed", "failed"} or
            not retest.get("finished_at") or manifest.get("head_sha") != value["source_head"] or
            retest.get("source_manifest_sha256") != file_sha(expected["source_manifest"])):
        raise ValueError("Full-flow evidence is not the authorized same-HEAD completed run")
    if ("plan_path" in value) != ("plan_sha256" in value):
        raise ValueError("User repair plan path and SHA must be supplied together")
    if "plan_path" in value:
        plan = _under(value["plan_path"], path.parent)
        if not plan.is_file() or file_sha(plan) != value["plan_sha256"]:
            raise ValueError("User repair plan evidence changed")
    return value


def validate_repair_profile(task: dict, runtime: Path) -> dict | None:
    profile = task.get("user_repair")
    if profile is None:
        return None
    if type(profile) is not dict or profile.get("status") != "active":
        raise ValueError("Invalid persisted user repair profile")
    request = load_repair_request(Path(profile["request_path"]), runtime)
    if (profile.get("request_sha256") != file_sha(profile["request_path"]) or request["task_id"] != task.get("id") or
            task.get("session_id") != request["target_session_id"] or profile.get("epoch") != request["id"] or
            any(profile.get(key) != request[key] for key in
                ("version", "id", "model", "reasoning_effort", "source_head", "source_full_flow_run", "rework_window", "max_cycles"))):
        raise ValueError("User repair profile differs from its explicit authorization")
    return profile


def _native_answer(path, session):
    from .full_flow_native import parse_full_flow_native
    answer = ue_checks._object(path, maximum=1024 * 1024)
    parent = Path(path).parent
    process = ue_checks._object(_under(parent / "process.json", parent))
    actual, terminal, errors, rate, model = parse_full_flow_native(_under(parent / "stdout.log", parent), session)
    if (process.get("exit_code") != 0 or process.get("timed_out") is not False or process.get("output_limit_exceeded") is not False or
            errors or (rate or {}).get("status") == "rejected" or not model["model_verified"] or terminal["is_error"] or
            answer.get("session_id") != actual or answer.get("answer") != terminal.get("result")):
        raise ValueError("Repair branch lacks completed native Opus 5.5/high evidence")
    return answer["answer"], model


def read_repair_session(task, runtime, out):
    """Opening identity only for killed repair turns; never claim completion."""
    from .providers import _strict_json
    profile = validate_repair_profile(task, runtime)
    if profile is None:
        return None
    directory = _under(out, Path(runtime) / "runs")
    if directory.parent != Path(runtime).absolute() / "runs":
        return None
    expected = task["session_id"]
    try:
        with _under(directory / "stdout.log", directory).open(encoding="utf-8-sig") as stream:
            for _ in range(64):
                line = stream.readline(1024 * 1024)
                if not line:
                    break
                if not line.strip():
                    continue
                event = _strict_json(line)
                if (type(event) is not dict or event.get("type") != "system" or event.get("session_id") != expected):
                    return None
                if event.get("subtype") == "init":
                    return expected if event.get("model") == MODEL else None
                if event.get("subtype") not in {"ui_invalidate", "task_notification", "task_updated", "background_tasks_changed", "task_started"}:
                    return None
    except (OSError, ValueError, UnicodeError):
        return None
    return None


class UserRepair:
    def _active_repair_profile(self, task):
        profile = validate_repair_profile(task, self.runtime)
        if profile and profile.get("activated_scope_sha256") != ue_checks.task_validation_scope_sha256(task, self.config["checks"]):
            raise ValueError("Active user repair scope changed without a new authorization")
        return profile

    def _user_repair_providers(self, task):
        profile = self._active_repair_profile(task)
        if profile is None or self._providers_injected:
            return self.providers
        from .providers import AgentProviders
        config = deepcopy(self.config)
        config["_user_repair_profile"] = deepcopy(profile)
        config.setdefault("claude", {}).update(model=MODEL, reasoning_effort="high")
        return AgentProviders(config)

    def _task_checks(self, task):
        checks = deepcopy(self.config["checks"])
        profile = self._active_repair_profile(task)
        if profile is None:
            return checks
        bridge = next((row for row in checks if row["id"] == "ue-task-validation"), {})
        argv = bridge.get("argv", [])
        if argv.count("--engine") != 1 or argv.index("--engine") + 1 >= len(argv):
            raise ValueError("User repair requires the trusted existing UE validation engine")
        if not argv or not isinstance(argv[0], str) or not Path(argv[0]).is_absolute():
            raise ValueError("User repair requires the trusted absolute validation interpreter")
        engine = argv[argv.index("--engine") + 1]
        if not Path(engine).is_absolute():
            raise ValueError("User repair requires an absolute trusted UE engine")
        # The controller may run under pythonw while maintenance uses python.
        # Bind the check to its configured interpreter, not the current host.
        checks.append({"id": CHECK_ID, "argv": [argv[0], str(Path(__file__).with_name("full_repair_check.py")),
            "--runtime", "{runtime}", "--workspace", "{workspace}", "--engine", engine],
            "timeout_seconds": 21600, "success_patterns": ["UE_FULL_FLOW_REPAIR_PASSED"],
            "failure_patterns": ["UE_FULL_FLOW_REPAIR_FAILED"]})
        return checks

    def _user_repair_gate(self, task, work, *, require_pass):
        profile = self._active_repair_profile(task)
        if profile is None:
            return True
        expected = task.get("candidate", {}).get("head_sha")
        rows = [row for row in task.get("check_results", []) if row.get("id") == CHECK_ID]
        if len(rows) != 1 or rows[0].get("head_sha") != expected:
            if require_pass:
                raise ValueError("User repair lacks its same-HEAD mandatory full-flow check")
            return False
        if not rows[0].get("passed"):
            if require_pass:
                raise ValueError("User repair full-flow check has not passed")
            return False
        from .full_repair_check import verify_repair_check
        proof = verify_repair_check(self.runtime, work, task, expected)
        if proof.get("passed") is not True:
            if require_pass:
                raise ValueError("User repair full-flow evidence is missing or no longer valid")
            return False
        return True

    def activate_user_repair(self, request_path):
        with self._locks():
            state = self._load()
            task = state.get("task") or {}
            request_path = Path(request_path).absolute()
            request = load_repair_request(request_path, self.runtime)
            digest = file_sha(request_path)
            if task.get("user_repair", {}).get("id") == request["id"]:
                validate_repair_profile(task, self.runtime)
                if task["user_repair"]["request_sha256"] != digest:
                    raise ValueError("Existing user repair authorization changed")
                self._write_repair_task_book(task)
                return self._snapshot(state)
            work = self.runtime / "workspace"
            if (state.get("phase") != "BLOCKED" or state.get("blocked_from") != "REVIEW" or state.get("operation") is not None or
                    task.get("kind") == "art" or task.get("id") != request["task_id"] or
                    task.get("coding_provider") != "claude" or self.config.get("coding_provider") != "claude" or
                    task.get("session_id") != request["source_session_id"] or
                    file_sha(self.state_path) != request["source_state_sha256"] or
                    ue_checks.task_validation_scope_sha256(task, self.config["checks"]) != request["source_scope_sha256"] or
                    gitops.head(work) != request["source_head"] or not gitops.clean(work) or
                    task.get("candidate", {}).get("head_sha") != request["source_head"]):
                raise ValueError("User repair activation does not match the stopped task, scope and clean candidate")
            review_path = _under(self.runtime / "tasks" / task["id"] / f"review-{task['reworks']}.json", self.runtime)
            review = ue_checks._object(review_path)
            if review.get("head_sha") != request["source_head"] or review.get("decision") not in {"accepted", "rework", "blocked"}:
                raise ValueError("User repair requires the actual latest same-HEAD review")
            directory = request_path.parent
            from .storage import lock
            # The operator's read-only plan holds this same lock until its
            # native process has returned; activation cannot race that branch.
            with lock(directory / "native.lock"):
                answer_path = Path(next(row["path"] for row in request["evidence"] if row["role"] == "model_answer"))
                _, model = _native_answer(answer_path, request["target_session_id"])
                plan = ""
                if request.get("plan_path"):
                    plan_path = Path(request["plan_path"])
                    if plan_path.name != "answer.json":
                        raise ValueError("The execution plan must be an actual native answer receipt")
                    plan, _ = _native_answer(plan_path, request["target_session_id"])
                before = deepcopy(state)
                previous = task.get("user_repair")
                if previous:
                    task.setdefault("user_repair_history", []).append(deepcopy(previous))
                supplement = ("\n\n用户新增授权（" + request["id"] + "）：\n" + request["user_instruction"] +
                    "\n完整上轮证据目录：" + request["source_full_flow_run"] +
                    "\n最终同一候选必须通过全量 M、至少原 19 项及本轮新增的正常 RHI 画面和正式打包；保留原核心测试及 Codex 独立审核。")
                task["body"] += supplement
                task.setdefault("acceptance_criteria", []).append("本次用户授权范围须通过固定全流程独立检查 ue-full-flow-repair，并保留所有原验收条件。")
                task["feedback"] = supplement + ("\n真实 Claude 自主方案原文：\n" + plan if plan else "")
                task.setdefault("session_history", []).append({"session_id": task["session_id"], "task_id": task["id"],
                    "retired_at": self.now().isoformat(), "reason": "explicit_user_repair", "authorization_id": request["id"]})
                task["session_id"] = None
                self._claim_session(task, request["target_session_id"])
                limit = self.config["max_reworks"] + task.get("manual_rework_allowance", 0) + task.get("autonomous_rework_allowance", 0)
                task["manual_rework_allowance"] = task.get("manual_rework_allowance", 0) + max(0, task["reworks"] + request["rework_window"] - limit)
                task["reworks"] += 1
                task["user_repair"] = {key: deepcopy(request[key]) for key in
                    ("version", "id", "model", "reasoning_effort", "source_head", "source_full_flow_run", "rework_window", "max_cycles")}
                task["user_repair"].update(request_path=str(request_path), request_sha256=digest, status="active",
                    epoch=request["id"], activation_rework=task["reworks"], activated_at=self.now().isoformat(),
                    activated_scope_sha256=ue_checks.task_validation_scope_sha256(task, self.config["checks"]))
                for key in ("plan_path", "plan_sha256"):
                    if key in request:
                        task["user_repair"][key] = request[key]
                task.update(coding_model=MODEL, coding_reasoning_effort="high", code_base=request["source_head"], code_resume=False, check_results=[])
                task.pop("validation_gate", None)
                task.setdefault("user_authorization_history", []).append({"id": request["id"], "request_sha256": digest,
                    "previous_reworks": before["task"]["reworks"], "previous_phase": before["phase"], "model_selection": model})
                state.update(phase="CODE", operation=None, failures=0, last_error=None)
                state.pop("blocked_from", None); state.pop("provider_retry", None)
                audit_path = directory / "activation.json"
                audit = {"version": 1, "request_sha256": digest, "before": before, "after": deepcopy(state)}
                if audit_path.exists():
                    if read_json(audit_path).get("request_sha256") != digest:
                        raise ValueError("Another authorization owns this activation receipt")
                else:
                    write_json(audit_path, audit)
                self._save(state)
                self._write_repair_task_book(task)
                return self._snapshot(state)

    def _write_repair_task_book(self, task):
        directory = self.runtime / "tasks" / task["id"]
        write_json(directory / "task.json", task)
        write_text(directory / "TASK.md", task["title"] + "\n\n" + task["body"] + "\n\n验收标准\n" +
                   "\n".join("- " + value for value in task["acceptance_criteria"]) + "\n\n原核心测试\n" +
                   "\n".join("- " + value for value in task.get("core_tests", [])))
