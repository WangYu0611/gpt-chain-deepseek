"""Bounded, durable user instructions handed to the existing coder at CHECK.

This module never configures tools or performs the requested project work.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from . import gitops, ue_checks
from .storage import lock, read_json, write_json, write_text
from .user_repair import MODEL, _under, file_sha, validate_repair_profile


def load_followup_request(request_path, runtime):
    runtime = Path(runtime).absolute()
    path = _under(request_path, runtime / "user-followups")
    value = ue_checks._object(path, maximum=128 * 1024)
    required = {"version", "id", "task_id", "repair_id", "repair_request_sha256", "session_id",
                "source_operation", "source_head", "user_instruction", "authorized_at", "max_turns"}
    if (set(value) != required or type(value["version"]) is not int or value["version"] != 1 or
            not re.fullmatch(r"[0-9a-f]{32}", str(value["id"])) or
            path != runtime / "user-followups" / value["id"] / "request.json" or
            type(value["max_turns"]) is not int or not 1 <= value["max_turns"] <= 3 or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", str(value["task_id"])) or
            not re.fullmatch(r"[0-9a-f]{40}", str(value["source_head"])) or
            not re.fullmatch(r"[0-9a-f]{64}", str(value["repair_request_sha256"])) or
            not isinstance(value["user_instruction"], str) or not value["user_instruction"].strip() or
            len(value["user_instruction"]) > 16000):
        raise ValueError("Invalid bounded user follow-up request")
    if datetime.fromisoformat(value["authorized_at"]).utcoffset() is None:
        raise ValueError("Follow-up authorization must include a timezone")
    op = value["source_operation"]
    if (not isinstance(op, dict) or op.get("kind") != "code" or op.get("task_id") != value["task_id"] or
            op.get("head_sha") != value["source_head"] or op.get("requested_session_id") != value["session_id"] or
            op.get("user_repair_id") != value["repair_id"] or
            op.get("user_repair_request_sha256") != value["repair_request_sha256"] or
            op.get("coding_model") != MODEL or op.get("coding_reasoning_effort") != "high"):
        raise ValueError("Follow-up source is not the authorized Opus CODE operation")
    out = _under(op.get("output_dir", ""), runtime / "runs")
    if out.parent != runtime / "runs" or not re.fullmatch(r"code-[0-9a-f]{32}", out.name):
        raise ValueError("Follow-up source output is not a managed CODE run")
    return value


def _identity(task, request, runtime):
    repair = validate_repair_profile(task, runtime)
    if (not repair or task.get("kind") == "art" or task.get("coding_provider") != "claude" or
            task.get("id") != request["task_id"] or task.get("session_id") != request["session_id"] or
            repair["id"] != request["repair_id"] or repair["request_sha256"] != request["repair_request_sha256"]):
        raise ValueError("Follow-up belongs to another task, repair authorization or native session")
    return repair


def validate_followup_profile(task, runtime):
    profile = task.get("user_followup")
    if profile is None:
        return None
    if not isinstance(profile, dict) or profile.get("status") not in {"active", "completed", "exhausted"}:
        raise ValueError("Invalid persisted user follow-up profile")
    request = load_followup_request(profile["request_path"], runtime)
    _identity(task, request, runtime)
    if (file_sha(profile["request_path"]) != profile.get("request_sha256") or
            any(profile.get(key) != request[key] for key in
                ("version", "id", "task_id", "repair_id", "repair_request_sha256", "session_id", "source_head", "max_turns"))):
        raise ValueError("Follow-up authorization changed")
    if profile.get("source_status") == "interrupted":
        path = _under(Path(profile["request_path"]).parent / "source-interruption.json", runtime)
        if file_sha(path) != profile.get("source_interruption_sha256"):
            raise ValueError("Follow-up source interruption checkpoint changed")
        proof = ue_checks._object(path, maximum=4 * 1024 * 1024)
        source = request["source_operation"]
        out = _under(source["output_dir"], Path(runtime) / "runs")
        if (proof.get("original_request_sha256") != profile["request_sha256"] or
                proof.get("original_operation") != source or proof.get("session_id") != profile["session_id"] or
                proof.get("source_status") != "interrupted" or proof.get("completed") is not False or
                proof.get("opening_verified") is not True or proof.get("model_verified") is not False or
                proof.get("recovery_record") not in task.get("code_timeout_recoveries", []) or
                file_sha(_under(out / "process.json", out)) != proof.get("process_sha256") or
                file_sha(_under(out / "stdout.log", out)) != proof.get("stdout_sha256")):
            raise ValueError("Follow-up interrupted source evidence no longer matches its original authorization")
    return profile


def _queue(runtime):
    path = _under(Path(runtime) / "user-followups.json", runtime)
    value = read_json(path) if path.exists() else {"version": 1, "requests": []}
    if value.get("version") != 1 or not isinstance(value.get("requests"), list):
        raise ValueError("Invalid user follow-up queue")
    return value


def completed_followup_evidence(task, runtime):
    """Read immutable completion proof without constructing a controller/provider."""
    runtime = Path(runtime).absolute()
    profile = validate_followup_profile(task, runtime)
    if not profile or profile["status"] != "completed":
        raise ValueError("Unfinished user follow-up cannot be reviewed or published")
    rows = [row for row in task.get("user_followup_results", []) if row["id"] == profile["id"] and row.get("completed")]
    if len(rows) != 1:
        raise ValueError("Follow-up lacks one verified completion receipt")
    row = rows[0]
    path = _under(row["receipt_path"], Path(profile["request_path"]).parent)
    if path.name != f"turn-{row['turn']}.json" or file_sha(path) != row["receipt_sha256"]:
        raise ValueError("Follow-up completion evidence changed")
    receipt = ue_checks._object(path, maximum=8 * 1024 * 1024)
    op, result = receipt.get("operation", {}), receipt.get("result", {})
    evidence = result.get("user_followup", {})
    stdout = _under(Path(op.get("output_dir", "")) / "stdout.log", runtime / "runs")
    if (receipt.get("id") != profile["id"] or receipt.get("task_id") != task["id"] or
            receipt.get("request_sha256") != profile["request_sha256"] or
            evidence.get("completed") is not True or
            evidence.get("evidence", {}).get("raw_stdout_sha256") != file_sha(stdout)):
        raise ValueError("Follow-up raw tool proof no longer matches its completion receipt")
    return deepcopy(evidence)


class UserFollowup:
    def queue_user_followup(self, request_path):
        """Queue independently of the long-running controller lock; never edit state."""
        request_path = Path(request_path).absolute()
        with lock(_under(self.runtime / "user-followups.lock", self.runtime)):
            request = load_followup_request(request_path, self.runtime)
            digest = file_sha(request_path)
            queue = _queue(self.runtime)
            existing = next((row for row in queue["requests"] if row["id"] == request["id"]), None)
            if existing:
                if existing["request_sha256"] != digest:
                    raise ValueError("Existing follow-up request changed")
                return deepcopy(existing)
            state = self._load()
            task = state.get("task") or {}
            self._active_repair_profile(task)
            _identity(task, request, self.runtime)
            if state.get("phase") == "CHECK":
                self._followup_source_proof(request, task)
                candidate = gitops.verify_candidate(self.runtime / "workspace", task["base_sha"], task["id"])
                if candidate["head_sha"] != task.get("candidate", {}).get("head_sha"):
                    raise ValueError("Completed source candidate changed")
            elif state.get("phase") != "CODE" or state.get("operation") != request["source_operation"]:
                raise ValueError("Follow-up must bind the running or just-completed CODE intent")
            if any(row.get("task_id") == task["id"] and row.get("status") in {"queued", "active"} for row in queue["requests"]):
                raise ValueError("This task already has a pending follow-up")
            row = {"id": request["id"], "task_id": task["id"], "request_path": str(request_path),
                   "request_sha256": digest, "status": "queued", "queued_at": self.now().isoformat()}
            queue["requests"].append(row)
            write_json(self.runtime / "user-followups.json", queue)
            return deepcopy(row)

    def _followup_queue_status(self, profile, status):
        with lock(_under(self.runtime / "user-followups.lock", self.runtime)):
            queue = _queue(self.runtime)
            rows = [row for row in queue["requests"] if row["id"] == profile["id"]]
            if len(rows) != 1 or rows[0]["request_sha256"] != profile["request_sha256"]:
                raise ValueError("Follow-up queue identity changed")
            rows[0].update(status=status, updated_at=self.now().isoformat())
            write_json(self.runtime / "user-followups.json", queue)

    def _followup_source_proof(self, request, task):
        from .full_flow_native import parse_full_flow_native
        op = request["source_operation"]
        out = _under(op["output_dir"], self.runtime / "runs")
        process_path = _under(out / "process.json", out)
        stdout = _under(out / "stdout.log", out)
        process = ue_checks._object(process_path)
        if process.get("timed_out") is True:
            return self._followup_interrupted_source(request, task, process, process_path, stdout)
        session, terminal, errors, rate, model = parse_full_flow_native(stdout, request["session_id"])
        if (process.get("exit_code") != 0 or process.get("timed_out") is not False or
                process.get("output_limit_exceeded") is not False or process.get("task_id") != request["task_id"] or
                process.get("head_sha") != op["head_sha"] or process.get("requested_session") != session or
                Path(process.get("output_dir", "")).absolute() != out or
                Path(process.get("workspace", "")).absolute() != self.runtime / "workspace" or
                process.get("strict_model_selection") is not True or process.get("native_protocol") != "full_flow_native_v1" or
                process.get("coding_model") != MODEL or process.get("coding_reasoning_effort") != "high" or
                process.get("user_repair_profile", {}).get("id") != request["repair_id"] or
                process.get("user_repair_profile", {}).get("request_sha256") != request["repair_request_sha256"] or
                errors or (rate or {}).get("status") == "rejected" or terminal.get("is_error") is not False or
                model.get("model_verified") is not True or process.get("model_selection") != model):
            raise ValueError("Follow-up source CODE lacks its actual completed Opus/session receipt")
        delivered = _under(self.runtime / "tasks" / task["id"] / f"delivery-{task['reworks']}.md", self.runtime)
        if (delivered.stat().st_size > 4 * 1024 * 1024 or task.get("delivery_text") != terminal.get("result") or
                delivered.read_text(encoding="utf-8-sig") != terminal.get("result") or
                datetime.fromisoformat(request["authorized_at"]).timestamp() > process_path.stat().st_mtime or
                datetime.fromisoformat(request["authorized_at"]) < datetime.fromisoformat(op["started_at"])):
            raise ValueError("Follow-up is not bound to the current delivery or was authorized after its completion")
        return {"process_sha256": file_sha(process_path), "stdout_sha256": file_sha(stdout), "model_selection": model,
                "process_receipt_mtime": datetime.fromtimestamp(process_path.stat().st_mtime, timezone.utc).isoformat(),
                "time_source": "process_receipt_filesystem_mtime", "delivery_sha256": file_sha(delivered)}

    def _followup_interrupted_source(self, request, task, process, process_path, stdout):
        """A genuine budget end may hand off a recovered commit, never fake a terminal."""
        from .user_repair import read_repair_session
        op = request["source_operation"]
        work = self.runtime / "workspace"
        out = self._code_timeout_receipt(task, work, op)
        operation_sha = self._replanning_digest(op)
        recoveries = [row for row in task.get("code_timeout_recoveries", [])
                      if row.get("operation_sha256") == operation_sha]
        if (len(recoveries) != 1 or recoveries[0].get("decision") != "recover_committed_code" or
                Path(recoveries[0].get("output_dir", "")).absolute() != out or
                Path(process.get("output_dir", "")).absolute() != out or
                Path(process.get("workspace", "")).absolute() != work or
                read_repair_session(task, self.runtime, out) != request["session_id"] or
                datetime.fromisoformat(request["authorized_at"]).timestamp() > process_path.stat().st_mtime or
                datetime.fromisoformat(request["authorized_at"]) < datetime.fromisoformat(op["started_at"])):
            raise ValueError("Follow-up interruption lacks its exact native timeout and committed-code recovery")
        candidate = gitops.verify_candidate(work, task["base_sha"], task["id"])
        if (candidate["head_sha"] != task.get("candidate", {}).get("head_sha") or
                candidate["head_sha"] == op.get("code_base", op["head_sha"]) or not gitops.clean(work)):
            raise ValueError("Interrupted source requires its recovered clean task candidate")
        gitops.git(work, "merge-base", "--is-ancestor", request["source_head"], candidate["head_sha"])
        request_path = self.runtime / "user-followups" / request["id"] / "request.json"
        return {"version": 1, "source_status": "interrupted", "completed": False,
                "opening_verified": True, "model_verified": False,
                "task_id": task["id"], "session_id": request["session_id"],
                "model": MODEL, "reasoning_effort": "high", "original_request_sha256": file_sha(request_path),
                "original_operation": deepcopy(op), "operation_sha256": operation_sha,
                "process_sha256": file_sha(process_path), "stdout_sha256": file_sha(stdout),
                "original_process_model_selection": deepcopy(process.get("model_selection")),
                "recovery_record": deepcopy(recoveries[0]), "recovery_sha256": self._replanning_digest(recoveries[0]),
                "candidate": candidate, "candidate_sha256": self._replanning_digest(candidate),
                "candidate_head_sha": candidate["head_sha"],
                "process_receipt_mtime": datetime.fromtimestamp(process_path.stat().st_mtime, timezone.utc).isoformat(),
                "time_source": "process_receipt_filesystem_mtime"}

    def _consume_user_followup(self, state, work):
        task = state["task"]
        previous = task.get("user_followup")
        if previous:
            profile = validate_followup_profile(task, self.runtime)
            if profile["status"] == "completed":
                self._followup_queue_status(profile, "completed")
            else:
                raise ValueError("Unfinished user follow-up cannot enter final CHECK")
        with lock(_under(self.runtime / "user-followups.lock", self.runtime)):
            rows = [row for row in _queue(self.runtime)["requests"] if row.get("task_id") == task["id"] and row.get("status") in {"queued", "active"}]
        if not rows:
            return False
        if len(rows) != 1 or state.get("operation") is not None:
            raise ValueError("Follow-up cannot interrupt an independent check or another request")
        row = rows[0]
        request = load_followup_request(row["request_path"], self.runtime)
        if file_sha(row["request_path"]) != row["request_sha256"]:
            raise ValueError("Queued follow-up bytes changed")
        self._active_repair_profile(task)
        _identity(task, request, self.runtime)
        proof = self._followup_source_proof(request, task)
        candidate = gitops.verify_candidate(work, task["base_sha"], task["id"])
        if candidate["head_sha"] != task["candidate"]["head_sha"]:
            raise ValueError("Follow-up candidate changed before its handoff")
        gitops.git(work, "merge-base", "--is-ancestor", request["source_head"], candidate["head_sha"])
        profile = {key: deepcopy(request[key]) for key in
                   ("version", "id", "task_id", "repair_id", "repair_request_sha256", "session_id", "source_head", "max_turns")}
        profile.update(request_path=row["request_path"], request_sha256=row["request_sha256"], status="active", activated_at=self.now().isoformat())
        if proof.get("source_status") == "interrupted":
            interruption_path = _under(Path(profile["request_path"]).parent / "source-interruption.json", self.runtime)
            if interruption_path.exists():
                if ue_checks._object(interruption_path, maximum=4 * 1024 * 1024) != proof:
                    raise ValueError("Existing source interruption checkpoint differs")
            else:
                write_json(interruption_path, proof)
            profile.update(source_status="interrupted", source_interruption_sha256=file_sha(interruption_path))
        if previous:
            task.setdefault("user_followup_history", []).append(deepcopy(previous))
        task["user_followup"] = profile
        task.setdefault("user_followup_attempts", [])
        task["feedback"] = task.get("feedback", "") + "\n\n用户追加指令（原文）：\n" + request["user_instruction"]
        task.update(code_base=candidate["head_sha"], code_resume=False, check_results=[])
        state.update(phase="CODE", operation=None)
        activation_path = Path(profile["request_path"]).parent / "activation.json"
        activation = {
            "version": 1, "request_sha256": profile["request_sha256"], "source_proof": proof,
            "candidate": candidate, "activated_at": profile["activated_at"]}
        if activation_path.exists():
            prior = ue_checks._object(_under(activation_path, activation_path.parent), maximum=4 * 1024 * 1024)
            if any(prior.get(key) != activation[key] for key in ("version", "request_sha256", "source_proof", "candidate")):
                raise ValueError("Follow-up activation checkpoint changed")
            profile["activated_at"] = prior["activated_at"]
        else:
            write_json(activation_path, activation)
        self._save(state)  # State owns consumption before the separate queue is marked.
        self._followup_queue_status(profile, "active")
        self._write_repair_task_book(task)
        return True

    def _followup_operation(self, state, op):
        task = state.get("task") or {}
        if op["kind"] != "code" or not task.get("user_followup"):
            return
        profile = validate_followup_profile(task, self.runtime)
        if profile["status"] == "completed":
            self._user_followup_gate(task)
            op.update(user_followup_id=profile["id"], user_followup_request_sha256=profile["request_sha256"])
            return
        if profile["status"] != "active":
            raise ValueError("Exhausted follow-up cannot start another native operation")
        attempts = [row for row in task.get("user_followup_attempts", []) if row["id"] == profile["id"]]
        if len(attempts) >= profile["max_turns"]:
            profile["status"] = "exhausted"
            self._block(state, "CODE", "用户追加指令已用完授权原生回合，仍缺少实际完成证据；等待处理。")
            self._save(state)
            self._followup_queue_status(profile, "exhausted")
            raise RuntimeError(state["last_error"])
        op.update(user_followup_id=profile["id"], user_followup_request_sha256=profile["request_sha256"], user_followup_turn=len(attempts) + 1)
        task["user_followup_attempts"].append({"id": profile["id"], "turn": op["user_followup_turn"],
            "output_dir": op["output_dir"], "started_at": op["started_at"]})

    def _followup_providers(self, task):
        profile = validate_followup_profile(task, self.runtime)
        repair = self._active_repair_profile(task)
        if self._providers_injected:
            return self.providers
        from .providers import AgentProviders
        config = deepcopy(self.config)
        config.update(_user_repair_profile=deepcopy(repair), _user_followup_profile=deepcopy(profile))
        config.setdefault("claude", {}).update(model=MODEL, reasoning_effort="high")
        return AgentProviders(config)

    def _user_repair_providers(self, task):
        if (task.get("user_followup") or {}).get("status") == "completed":
            self._user_followup_gate(task)
            return self._followup_providers(task)
        return super()._user_repair_providers(task)

    def _followup_failure_limit(self, state, error):
        task = state.get("task") or {}
        profile = task.get("user_followup") or {}
        if profile.get("status") != "active":
            return False
        attempts = [row for row in task.get("user_followup_attempts", []) if row["id"] == profile["id"]]
        if len(attempts) < profile["max_turns"]:
            return False
        profile["status"] = "exhausted"
        task.setdefault("user_followup_errors", []).append({"id": profile["id"], "error": f"{type(error).__name__}: {error}",
            "operation": deepcopy(state.get("operation")), "at": self.now().isoformat()})
        self._block(state, "CODE", "用户追加指令已达到授权回合上限且本轮未成功，等待处理；原始错误：" + str(error))
        self._save(state)
        self._followup_queue_status(profile, "exhausted")
        return True

    def _user_followup_code(self, state, work):
        task = state["task"]
        profile = validate_followup_profile(task, self.runtime)
        if profile["status"] != "active":
            self._block(state, "CODE", "用户追加指令已耗尽回合，不能隐式继续或绕过验收。")
            return
        request = load_followup_request(profile["request_path"], self.runtime)
        if state.get("operation") is None and (not gitops.clean(work) or gitops.head(work) != task["code_base"]):
            raise ValueError("Follow-up starts only from its clean candidate")
        prompt = self._base_prompt() + "\n继续同一任务和原生会话，处理用户新增指令；方案、配置及验证由你自主完成。\n" + request["user_instruction"]
        if profile.get("source_status") == "interrupted":
            prompt += ("\n前一 CODE 调用实际达到外层时限，没有完整终态；已保留其超时回执并从真实 Git 提交恢复干净候选。"
                       "控制器仅核实了原会话/模型开头，不将旧调用标成完成。请先核对已有工作与未完成事项，再继续本用户指令；"
                       "仍使用原来的有限交接回合，原任务全部最终检查保持不变。")
        prompt += ("\n这是有界的追加交接，最多 " + str(profile["max_turns"]) + " 个原生回合（含失败尝试）。"
            "每个回合先检查已有工作，不重复已完成操作；需要下一回合加载配置时如实交付。"
            "保留原任务全部业务验收和最终独立全流程检查，不把工具接通当作游戏测试通过。"
            "允许没有新的代码提交；如实际修改仓库，提交标题仍必须仅为任务编号，交付时保持工作区干净。"
            "不要为满足提交规则制造改动。不更改控制器状态、可信授权或验收定义。\n任务上下文：\n" +
            json.dumps(task, ensure_ascii=False))
        result = self._invoke(state, "code", prompt, work, providers=self._followup_providers(task))
        if (result.get("completed") is not True or result.get("session_id") != profile["session_id"] or
                result.get("model_selection", {}).get("model_verified") is not True):
            raise ValueError("Follow-up lacks a completed same-session verified Opus native turn")
        candidate = gitops.verify_candidate(work, task["base_sha"], task["id"])
        op = deepcopy(state["operation"])
        gitops.git(work, "merge-base", "--is-ancestor", op["head_sha"], candidate["head_sha"])
        evidence = result.get("user_followup")
        if not isinstance(evidence, dict) or type(evidence.get("completed")) is not bool or not isinstance(evidence.get("evidence"), dict):
            raise ValueError("Follow-up provider did not return actual tool evidence")
        path = Path(profile["request_path"]).parent / f"turn-{op['user_followup_turn']}.json"
        receipt = {"version": 1, "id": profile["id"], "task_id": task["id"], "request_sha256": profile["request_sha256"],
                   "operation": op, "candidate": candidate, "result": result, "finished_at": self.now().isoformat()}
        if path.exists() and read_json(path).get("result") != result:
            raise ValueError("Completed follow-up turn receipt changed")
        if not path.exists():
            write_json(path, receipt)
        delivery = path.with_suffix(".md")
        write_text(delivery, result.get("text", ""))
        task.setdefault("user_followup_results", []).append({"id": profile["id"], "turn": op["user_followup_turn"],
            "receipt_path": str(path), "receipt_sha256": file_sha(path), "completed": evidence["completed"]})
        task["delivery_text"] = task.get("delivery_text", "") + f"\n\n用户追加交接第 {op['user_followup_turn']} 轮：\n" + result.get("text", "")
        task.update(candidate=candidate, code_base=candidate["head_sha"], code_resume=False, check_results=[])
        state["operation"] = None
        if evidence["completed"]:
            profile["status"] = "completed"
            state["phase"] = "CHECK"
        elif op["user_followup_turn"] >= profile["max_turns"]:
            profile["status"] = "exhausted"
            self._block(state, "CODE", "用户追加指令达到授权回合上限，仍无实际工具完成证据；等待处理，未进入最终验收。")
        self._save(state)
        self._followup_queue_status(profile, profile["status"])
        self._write_repair_task_book(task)

    def _user_followup_gate(self, task):
        if task.get("user_followup"):
            completed_followup_evidence(task, self.runtime)
