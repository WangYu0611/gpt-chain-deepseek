"""Durable serial state machine: plan -> code -> check -> review -> publish."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import uuid

from github_harness_lab.lab_harness.process import run_process, safe_environment
from .calendar import WorkCalendar
from . import gitops
from .providers import AgentProviders, ProviderError, codex_result_directory, read_session, validate_output
from .storage import is_windows_sharing_error, lock, read_json, write_json, write_text
from .art_autonomy import ArtAutonomy
from .task_replanning import TaskReplanning
from .user_repair import UserRepair
from .user_followup import UserFollowup


_PROVIDER_RETRY_REASONS = frozenset({"server_unavailable", "rate_limited", "transport", "workspace_routing_network",
                                     "local_cli_unavailable", "claude_subscription_limit", "codex_subscription_limit"})


def utcnow():
    return datetime.now(timezone.utc)


class Engine(ArtAutonomy, TaskReplanning, UserFollowup, UserRepair):
    def __init__(self, config: dict, providers=None, *, now=None):
        self.config = config
        self.runtime = Path(config["runtime_dir"])
        self.project = Path(config["project_path"])
        self.state_path = self.runtime / "state.json"
        self.providers = providers if providers is not None else AgentProviders(config)
        self._providers_injected = providers is not None
        self.now = now or utcnow

    def _calendar(self):
        # Reload between every step, so an annual calendar can be updated live.
        return WorkCalendar(self.config["schedule"], base_dir=Path(self.config["config_path"]).parent)

    def _binding(self):
        values = {key: self.config.get(key) for key in
                  ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")}
        # Preserve pre-milestone checkpoints byte-for-byte when no new
        # publication policy is present. Adding/changing one still rebinds it.
        if "publication" in self.config:
            values["publication"] = self.config["publication"]
        return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _publication(self):
        publication = self.config.get("publication")
        return publication if publication and publication.get("mode") == "milestone" else None

    def _pending_milestone_tasks(self, state):
        publication = self._publication()
        milestone_id = publication["milestone_id"] if publication else None
        archived = set()
        for history in state.get("milestone_history", []):
            archived.update(history.get("accepted_task_ids", []))
        return [record for record in state["accepted"]
                if record.get("publication", {}).get("local_only") is True and not record.get("milestone_publication") and
                (record["publication"].get("deferred_to_milestone") == milestone_id or
                 (record["publication"].get("deferred_to_milestone") is None and record["id"] not in archived))]

    def _deferred_tasks(self, state):
        publication = self._publication()
        milestone_id = publication["milestone_id"] if publication else None
        return [record for record in state.get("deferred", [])
                if record.get("milestone_id") == milestone_id]

    def _load(self):
        if not self.state_path.exists():
            return {"version": 1, "binding": self._binding(), "phase": "PLAN", "sequence": 1,
                    "task": None, "accepted": [], "operation": None, "failures": 0,
                    "last_error": None, "expected_head": None}
        state = read_json(self.state_path)
        if state.get("version") != 1 or state.get("binding") != self._binding():
            raise ValueError("运行状态与项目/目标/分支/验收配置不匹配，请保留断点并使用原配置。")
        return state

    def _save(self, state):
        state["updated_at"] = self.now().isoformat()
        write_json(self.state_path, state)

    def _snapshot(self, state):
        schedule = self._calendar().describe(self.now())
        paused = (self.runtime / "PAUSE").exists()
        retry = state.get("provider_retry")
        status = "paused" if paused else state["phase"].lower()
        if (not paused and state["phase"] not in {"BLOCKED", "DONE"} and isinstance(retry, dict) and
                retry.get("status") == "waiting" and schedule.get("allowed")):
            status = "waiting_provider"
        return {"status": status, "phase": state["phase"],
                "paused": paused, "task_id": state["task"]["id"] if state["task"] else None,
                "accepted_count": len(state["accepted"]), "failures": state["failures"],
                "last_error": state.get("last_error"), "schedule": schedule,
                "workspace": str(self.runtime / "workspace"), "runtime_dir": str(self.runtime),
                "updated_at": state.get("updated_at"),
                "milestone_id": self._publication()["milestone_id"] if self._publication() else None,
                "milestone_feedback": state.get("milestone_feedback"),
                "provider_retry": deepcopy(retry),
                "task_kind": (state.get("task") or {}).get("kind", "code") if state.get("task") else None,
                "coding_model": (state.get("task") or {}).get("coding_model"),
                "art_pending": [{key: item.get(key) for key in ("id", "title", "status", "review_package", "review_package_sha256")}
                                for item in state.get("art_pending", [])],
                "art_accepted_count": len(state.get("art_history", []))}

    def status(self):
        return self._snapshot(self._load())

    def art_queue(self):
        """Read the independent inbox without changing the active coding task."""
        path = self.runtime / "art-queue.json"
        queue = read_json(path) if path.exists() else {"version": 1, "requests": []}
        if queue.get("version") != 1 or not isinstance(queue.get("requests"), list):
            raise ValueError("美术任务队列格式无效。")
        return deepcopy(queue)

    def enqueue_art(self, task_id, title="普通住宅三级美术样板", body="按已确认风格制作、处理并展示 L1/L4/L8，提交独立技术检查和用户视觉验收。"):
        """Persist an authorized request; dispatch occurs only at a clean PLAN boundary."""
        settings = self.config.get("art_pipeline")
        if not settings or task_id not in settings["task_ids"]:
            raise ValueError("美术任务编号必须位于 art_pipeline.task_ids 白名单中。")
        if not all(isinstance(value, str) and value.strip() for value in (task_id, title, body)):
            raise ValueError("美术任务编号、标题和任务书不能为空。")
        config_file = Path(settings["config_file"])
        request = {"id": task_id, "kind": "art", "title": title.strip(), "body": body.strip(),
                   "config_file": str(config_file), "config_sha256": hashlib.sha256(config_file.read_bytes()).hexdigest(),
                   "artifacts_dir": settings["artifacts_dir"], "model": "claude-opus-5-5",
                   "reasoning_effort": "high", "status": "queued", "enqueued_at": self.now().isoformat()}
        with lock(self.runtime / "art-queue.lock"):
            queue = self.art_queue()
            previous = next((item for item in queue["requests"] if item["id"] == task_id), None)
            if previous is not None:
                if any(previous.get(key) != request[key] for key in ("title", "body", "config_file", "config_sha256", "artifacts_dir")):
                    raise ValueError("已有美术任务不能被重新入队覆盖；请使用新任务编号或提交该任务的验收决定。")
                return {"status": previous["status"], "request": deepcopy(previous), "already_queued": True}
            queue["requests"].append(request)
            write_json(self.runtime / "art-queue.json", queue)
        return {"status": "queued", "request": request, "already_queued": False}

    def resolve_art(self, task_id, decision, feedback, review_package_sha256):
        """Queue a visual decision against one exact review package, without interrupting code."""
        if decision not in {"accepted", "rework"} or not isinstance(feedback, str) or not feedback.strip():
            raise ValueError("美术验收必须为 accepted/rework，并记录明确意见。")
        if not isinstance(review_package_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", review_package_sha256):
            raise ValueError("美术验收必须绑定完整 review-package SHA-256。")
        with lock(self.runtime / "art-queue.lock"):
            queue = self.art_queue()
            item = next((entry for entry in queue["requests"] if entry["id"] == task_id), None)
            if not item or item.get("status") != "awaiting_approval" or item.get("review_package_sha256") != review_package_sha256:
                raise ValueError("任务尚未提交人工验收，或验收包版本已改变。")
            package = Path(item["artifacts_dir"]) / "review-package.json"
            if hashlib.sha256(package.read_bytes()).hexdigest() != review_package_sha256:
                raise ValueError("验收包已变化，请重新检查当前版本。")
            if decision == "accepted":
                self._refresh_art_evidence(item, item)
            resolution = {"decision": decision, "feedback": feedback.strip(),
                          "review_package_sha256": review_package_sha256, "recorded_at": self.now().isoformat()}
            if item.get("decision"):
                if any(item["decision"].get(key) != resolution[key] for key in ("decision", "feedback", "review_package_sha256")):
                    raise ValueError("已有待处理验收决定，不能覆盖。")
            else:
                item["decision"] = resolution
                write_json(self.runtime / "art-queue.json", queue)
        return {"status": "decision_queued", "task_id": task_id, "decision": decision,
                "review_package_sha256": review_package_sha256}

    def _dispatch_art(self, state, work):
        if state.get("task") is not None or state.get("operation") is not None or state.get("planning_selection"):
            return False
        if not (self.runtime / "art-queue.json").exists():
            return False
        with lock(self.runtime / "art-queue.lock"):
            queue = self.art_queue()
            pending = state.setdefault("art_pending", [])
            for history in state.get("art_history", []):
                old = next((entry for entry in queue["requests"] if entry["id"] == history["id"]), None)
                if old is not None:
                    old["status"] = "accepted"
            selected = None
            for record in list(pending):
                item = next((entry for entry in queue["requests"] if entry["id"] == record["id"]), None)
                if item is None:
                    raise RuntimeError("待验收美术任务丢失队列记录。")
                item.update(status="awaiting_approval", **{key: record[key] for key in (
                    "review_package", "review_package_sha256", "independent_check", "independent_check_sha256")})
                decision = item.get("decision")
                if not decision:
                    continue
                if decision["review_package_sha256"] != record["review_package_sha256"]:
                    raise RuntimeError("美术验收决定与持久验收包不匹配。")
                if hashlib.sha256(Path(record["review_package"]).read_bytes()).hexdigest() != record["review_package_sha256"]:
                    raise RuntimeError("待处理的美术验收包已改变。")
                if decision["decision"] == "accepted":
                    self._refresh_art_evidence(record["task"]["art_request"], record)
                    state.setdefault("art_history", []).append({**record, "status": "accepted", "visual_decision": decision})
                    pending.remove(record)
                    item.update(status="accepted", accepted_at=self.now().isoformat())
                elif selected is None:
                    selected = deepcopy(record["task"])
                    selected["feedback"] = decision["feedback"]
                    selected.setdefault("visual_feedback_history", []).append(deepcopy(decision))
                    selected["reworks"] += 1
                    selected["code_resume"] = True
                    selected["check_results"] = []
                    selected.pop("art_review", None)
                    pending.remove(record)
                    item.setdefault("decision_history", []).append(item.pop("decision"))
            if selected is None:
                item = next((entry for entry in queue["requests"] if entry.get("status") == "queued"), None)
                if item:
                    selected = {"id": item["id"], "kind": "art", "title": item["title"], "body": item["body"],
                                "art_request": deepcopy(item), "coding_provider": "claude",
                                "coding_model": "claude-opus-5-5", "coding_reasoning_effort": "high",
                                "acceptance_criteria": ["独立资产检查通过", "Codex 技术审阅通过", "用户确认固定版本的美术验收包"],
                                "reworks": 0, "session_id": None, "feedback": "", "code_resume": False, "check_results": []}
            if selected is not None:
                current = gitops.head(work)
                selected.update(base_sha=current, code_base=current, candidate={"head_sha": current})
                self._validate_art_task(selected, work)
                directory = self.runtime / "tasks" / selected["id"]
                write_json(directory / "task.json", selected)
                write_text(directory / "TASK.md", selected["id"] + " " + selected["title"] + "\n\n" + selected["body"] +
                           "\n\n执行：Claude Opus 5.5 / high。资产生产不得更改主项目源码或提交。\n")
                state.update(task=selected, phase="CODE", operation=None, failures=0, last_error=None)
                entry = next(entry for entry in queue["requests"] if entry["id"] == selected["id"])
                entry.update(status="active", dispatched_at=self.now().isoformat())
            elif state.get("art_return_done") is not None:
                previous = state.pop("art_return_done")
                state["phase"] = "DONE"
                if previous.get("completion_reason") is not None:
                    state["completion_reason"] = previous["completion_reason"]
            self._save(state)  # State is authoritative if the following queue update is interrupted.
            write_json(self.runtime / "art-queue.json", queue)
            return selected is not None

    def pause(self):
        write_text(self.runtime / "PAUSE", self.now().isoformat() + "\n")
        return self.status()

    def resume(self):
        (self.runtime / "PAUSE").unlink(missing_ok=True)
        return self.status()

    def advance_milestone(self, next_config: dict, reason: str):
        """Explicitly advance planning while keeping the reviewed stage pending manual confirmation."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("阶段切换必须记录用户明确授权的原因。")
        if not isinstance(next_config, dict):
            raise ValueError("下一阶段需要完整配置。")
        next_config = deepcopy(next_config)
        immutable = ("runtime_dir", "project_path", "branch", "base_ref", "checks", "push", "remote", "task_prefix")
        for key in immutable:
            if next_config.get(key) != self.config.get(key):
                raise ValueError("阶段切换不能改变 " + key + "。")
        publication, following = self._publication(), next_config.get("publication")
        if (not publication or not isinstance(following, dict) or set(following) != {"mode", "milestone_id", "goal"} or
                following.get("mode") != "milestone" or not isinstance(following.get("milestone_id"), str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}", following["milestone_id"]) or
                following["milestone_id"] == publication["milestone_id"] or
                not isinstance(following.get("goal"), str) or not following["goal"].strip() or
                not isinstance(next_config.get("goal"), str) or not next_config["goal"].strip()):
            raise ValueError("阶段切换需要新的里程碑编号及明确目标。")
        with self._locks():
            state = self._load()
            if (state["phase"] != "BLOCKED" or state.get("blocked_from") != "MILESTONE_REVIEW" or
                    state.get("task") is not None or state.get("operation") is not None):
                raise ValueError("只允许从无活动任务或操作的阶段验收阻塞切换。")
            history = state.get("milestone_history", [])
            if any(entry.get("milestone", {}).get("id") == following["milestone_id"] for entry in history):
                raise ValueError("下一阶段编号已存在于阶段历史。")
            milestone = self._milestone_candidate(state, self.runtime / "workspace")
            expected = milestone["head_sha"]
            checks = self.config["checks"] + milestone.get("milestone_checks", [])
            results = milestone.get("check_results", [])
            if (not checks or len(results) != len(checks) or
                    any(result.get("id") != check["id"] or result.get("head_sha") != expected or result.get("passed") is not True
                        for result, check in zip(results, checks))):
                raise ValueError("阶段切换需要绑定当前 HEAD 的全部独立检查通过。")
            review = validate_output("review", milestone.get("review"))
            if review["decision"] not in {"blocked", "accepted"} or review["head_sha"] != expected:
                raise ValueError("阶段切换需要绑定当前 HEAD 的独立阶段审核。")
            review_path = self.runtime / "milestones" / milestone["id"] / f"review-{milestone['attempt']}.json"
            if Path(milestone.get("review_path", "")).resolve() != review_path.resolve() or read_json(review_path) != review:
                raise ValueError("阶段审核原始回执与断点不一致。")
            following_engine = Engine(next_config, self.providers, now=self.now)
            audit_path = self.runtime / "milestones" / milestone["id"] / (
                "advance-to-" + following["milestone_id"] + "-" + uuid.uuid4().hex + ".json")
            transition = {"from": milestone["id"], "to": following["milestone_id"], "head_sha": expected,
                          "reason": reason.strip(), "authorized_at": self.now().isoformat(),
                          "from_binding": state["binding"], "to_binding": following_engine._binding(),
                          "next_goal": following["goal"], "audit_path": str(audit_path)}
            archive = {"milestone": deepcopy(milestone), "status": "pending_manual_confirmation",
                       "accepted_task_ids": [record["id"] for record in state["accepted"]], "transition": transition}
            state.setdefault("milestone_history", []).append(archive)
            for key in ("milestone", "milestone_feedback", "blocked_from", "provider_retry", "planning_selection", "completion_reason"):
                state.pop(key, None)
            state.update(phase="PLAN", task=None, operation=None, failures=0, last_error=None,
                         binding=transition["to_binding"])
            snapshot = following_engine._snapshot(state)
            write_json(audit_path, archive)
            self._save(state)
            self.config = next_config
            snapshot.update(updated_at=state["updated_at"], milestone_transition=deepcopy(transition))
            return snapshot

    def _locks(self):
        stack = ExitStack()
        try:
            stack.enter_context(lock(gitops.common_dir(self.project) / "unattended-controller.lock"))
            stack.enter_context(lock(self.runtime / "controller.lock"))
        except BaseException:
            stack.close()
            raise
        return stack

    def _verified_task_validation_gate(self, state, work):
        from .ue_checks import _is_reparse, _object, _safe_under, verify_task_validation_policy
        task = state["task"]
        gate = verify_task_validation_policy(self.config, task, self.runtime, work)
        if gate is None:
            return None
        review_path = _safe_under(self.runtime, Path("tasks") / task["id"] / f"review-{task['reworks']}.json")
        review = validate_output("review", _object(review_path))
        if review["decision"] != "accepted" or review["head_sha"] != gate["head_sha"]:
            raise ValueError("门槛协调必须有最新同轮、同 HEAD 的独立 accepted 审核。")
        config_path = Path(self.config["config_path"])
        if any(_is_reparse(path) for path in (config_path, *config_path.parents)):
            raise ValueError("门槛策略配置不能通过链接或重解析点读取。")
        raw_config = config_path.read_bytes()
        if json.loads(raw_config.decode("utf-8-sig")).get("task_validation_policy") != gate["policy"]:
            raise ValueError("磁盘上的可信门槛策略已改变。")
        gate.update(review_sha256=hashlib.sha256(review_path.read_bytes()).hexdigest(),
                    config_sha256=hashlib.sha256(raw_config).hexdigest(), reworks=task["reworks"])
        return gate

    def _record_task_validation_gate(self, state, gate):
        from .ue_checks import _safe_under, _object
        task = state["task"]
        path = _safe_under(self.runtime, Path("tasks") / task["id"] /
                           f"validation-gate-{task['reworks']}-{gate['policy_sha256']}.json")
        record = {**gate, "verified_at": self.now().isoformat()}
        if path.exists():
            previous = _object(path, 16 * 1024 * 1024)
            record["verified_at"] = previous.get("verified_at")
            if previous != record:
                raise ValueError("已存在的门槛审计与当前证据不一致。")
        else:
            write_json(path, record)
        # The audit precedes state; a crash retries this exact audit idempotently.
        task["validation_gate"] = {**record, "audit_path": str(path)}

    def reconcile_task_validation(self):
        """Explicitly reconcile a pinned accepted task; no model, retry, or publish."""
        with self._locks():
            state = self._load()
            task = state.get("task")
            resumed = state.get("phase") == "PUBLISH" and bool((task or {}).get("validation_gate"))
            if (not task or task.get("kind") == "art" or state.get("operation") is not None or
                    not (resumed or (state.get("phase") == "BLOCKED" and state.get("blocked_from") == "REVIEW"))):
                raise ValueError("门槛协调仅允许已阻塞的当前功能任务 REVIEW。")
            gate = self._verified_task_validation_gate(state, self.runtime / "workspace")
            if not gate or gate.get("status") != "policy_verified" or gate.get("gate_passed") is not True:
                raise ValueError("没有可核实的可信任务门槛策略。")
            self._record_task_validation_gate(state, gate)
            state.update(phase="PUBLISH", operation=None, last_error=None)
            state.pop("blocked_from", None)
            self._save(state)
            return self._snapshot(state)

    def retry(self, *, rework=False, new_session=False, external_validation=False, replan=False):
        if replan and (rework or new_session or external_validation):
            raise ValueError("重新规划不能与直接返工、重建会话或外层补证同时恢复。")
        if new_session and not rework:
            raise ValueError("--new-session 必须配合 --rework 使用。")
        if external_validation and rework:
            raise ValueError("外层补证与编码返工不能同时恢复。")
        with self._locks():
            state = self._load()
            if state["phase"] != "BLOCKED":
                raise ValueError("只有 BLOCKED 状态需要 retry；暂停请使用 resume。")
            if replan:
                self._retry_task_replanning(state)
                self._save(state)
                return self._snapshot(state)
            if external_validation:
                self._retry_external_validation(state)
                self._save(state)
                return self._snapshot(state)
            if rework:
                self._manual_rework(state, new_session=new_session)
                self._save(state)
                return self._snapshot(state)
            recovered_operation = self._retry_completed_operation(state)
            state["phase"] = state.pop("blocked_from", "PLAN")
            state["failures"] = 0
            state["last_error"] = None
            state["operation"] = recovered_operation
            state.pop("provider_retry", None)
            if state["phase"] == "CODE":
                state["task"]["code_resume"] = True
            self._save(state)
        return self.status()

    def _retry_completed_operation(self, state):
        """Keep only a complete planning/review receipt still bound to this task/HEAD."""
        phase, op = state.get("blocked_from"), state.get("operation")
        task = state.get("task") or {}
        if phase == "CODE" and (task.get("user_followup") or {}).get("status") == "active" and op:
            self._validate_operation_task(state)
            recovered = self._followup_providers(task).recover("code", Path(op["output_dir"]))
            return op if isinstance(recovered, dict) and recovered.get("completed") is True else None
        if phase == "CODE" and task.get("kind") == "art" and op and op.get("kind") == "code":
            self._validate_operation_task(state)
            work = self.runtime / "workspace"
            self._recover_art_recipe_commit(state, work)
            self._validate_art_task(task, work, allow_recipe_dirty=True)
            recovered = self._art_providers(task).recover("code", Path(op["output_dir"]))
            return op if isinstance(recovered, dict) and recovered.get("completed") is True else None
        if phase not in {"PLAN", "REPLAN", "REVIEW", "ART_REVIEW"} or not op or op.get("kind") != self._provider_kind(phase):
            return None
        task = state.get("task") or {}
        if op.get("task_id") != task.get("id"):
            return None
        work = self.runtime / "workspace"
        current = gitops.head(work)
        if op.get("head_sha") != current or (phase != "REPLAN" and not gitops.clean(work)):
            return None
        if phase == "REPLAN":
            pending = task.get("replanning") or {}
            if pending.get("status") != "pending" or self._replanning_source(task, work) != pending.get("fingerprint"):
                return None
        if phase in {"REVIEW", "ART_REVIEW"} and task.get("candidate", {}).get("head_sha") != current:
            return None
        try:
            providers = self._user_repair_providers(task) if op["kind"] == "code" else self.providers
            recovered = providers.recover(op["kind"], Path(op["output_dir"]))
        except ProviderError:
            return None
        if recovered is None or (phase in {"REVIEW", "ART_REVIEW"} and recovered.get("head_sha") != current):
            return None
        return op

    def defer(self, reason):
        """Explicitly carry checked candidate code forward without accepting its task."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("延期必须提供明确原因。")
        with self._locks():
            state = self._load()
            task = state.get("task")
            if state.get("phase") != "BLOCKED" or state.get("blocked_from") != "REVIEW" or not task:
                raise ValueError("只允许延期 REVIEW 阻塞中的当前任务。")
            if any(record["id"] == task["id"] for record in state.get("deferred", [])):
                raise ValueError("该任务已延期，不能重复延期。")
            work = self.runtime / "workspace"
            current = gitops.head(work)
            if current != task.get("candidate", {}).get("head_sha") or not gitops.clean(work):
                raise ValueError("延期要求当前干净 HEAD 与候选提交一致。")
            op = state.get("operation")
            if op and (op.get("kind") != "review" or op.get("task_id") != task["id"] or op.get("head_sha") != current):
                raise ValueError("延期不能沿用其他任务或 HEAD 的操作。")
            directory = self.runtime / "tasks" / task["id"]
            reviews = [(int(p.stem[7:]), p) for p in directory.glob("review-*.json") if p.stem[7:].isdigit()]
            if not reviews:
                raise ValueError("延期缺少最新验收回执。")
            number, review_path = max(reviews)
            review = validate_output("review", read_json(review_path))
            if (number != task["reworks"] or review.get("decision") not in {"blocked", "rework"}
                    or review.get("head_sha") != current):
                raise ValueError("延期要求最新同轮 blocked/rework 回执绑定当前 HEAD。")
            checks, results = self.config["checks"], task.get("check_results", [])
            if (not checks or len(results) != len(checks) or
                    any(result.get("id") != check["id"] or result.get("head_sha") != current or
                        result.get("passed") is not True for result, check in zip(results, checks))):
                raise ValueError("延期要求全部配置检查通过并绑定当前 HEAD。")
            path = directory / "deferred-task.json"
            self._claim_session(task, task.get("session_id"))
            publication = self._publication()
            record = {"id": task["id"], "title": task["title"], "base_sha": task["base_sha"],
                      "head_sha": current, "reason": reason.strip(), "deferred_at": self.now().isoformat(),
                      "status": "pending_manual_confirmation", "receipt_path": str(path),
                      "milestone_id": publication["milestone_id"] if publication else None}
            snapshot = {**record, "task": deepcopy(task), "review": review, "review_path": str(review_path)}
            if path.exists():
                saved = read_json(path)
                deferred_at = saved.get("deferred_at") if isinstance(saved, dict) else None
                if (not isinstance(deferred_at, str) or not deferred_at or
                        datetime.fromisoformat(deferred_at).utcoffset() is None or
                        saved != {**snapshot, "deferred_at": deferred_at}):
                    raise ValueError("已有延期回执与当前任务、原验收、HEAD或延期原因不匹配。")
                # The receipt is committed before state. Reuse its exact time
                # and bytes after a crash; never issue work or rewrite history.
                record["deferred_at"] = deferred_at
            else:
                write_json(path, snapshot)
            state.setdefault("deferred", []).append(record)
            state.update(expected_head=current, sequence=state["sequence"] + 1, task=None,
                         operation=None, phase="PLAN", failures=0, last_error=None)
            state.pop("blocked_from", None)
            state.pop("provider_retry", None)
            self._save(state)
        return self.status()

    def _retry_external_validation(self, state):
        """Recheck an existing rejected candidate after a no-commit handoff."""
        from . import ue_checks

        task = state.get("task")
        if state.get("blocked_from") != "CODE" or not task or state.get("operation"):
            raise ValueError("外层补证恢复要求 CODE 阻塞且没有未完成操作。")
        work = self.runtime / "workspace"
        current = gitops.head(work)
        if (current != task.get("candidate", {}).get("head_sha") or
                current != task.get("code_base") or not gitops.clean(work)):
            raise ValueError("外层补证要求干净 HEAD 与现有候选提交、编码基线一致。")
        if gitops.verify_candidate(work, task["base_sha"], task["id"]) != task["candidate"]:
            raise ValueError("现有候选提交与本任务不匹配。")
        directory = ue_checks._safe_under(self.runtime, Path("tasks") / task["id"])
        reviews = [(int(p.stem[7:]), p) for p in directory.glob("review-*.json") if p.stem[7:].isdigit()]
        if not reviews:
            raise ValueError("外层补证恢复缺少原始返工审核回执。")
        number, review_path = max(reviews)
        review = ue_checks._object(ue_checks._safe_under(directory, review_path.name))
        if (number != task["reworks"] - 1 or review.get("decision") not in {"rework", "blocked"} or
                review.get("head_sha") != current):
            raise ValueError("外层补证要求上一轮返工审核绑定当前候选提交。")
        failures = task.get("code_failures") or []
        if not failures:
            raise ValueError("外层补证恢复缺少编码交接失败证据。")
        failure_path = Path(failures[-1])
        try:
            relative = failure_path.relative_to(self.runtime / "runs")
        except ValueError as error:
            raise ValueError("编码交接失败证据必须位于本运行目录。") from error
        failure = ue_checks._object(ue_checks._safe_under(self.runtime / "runs", relative))
        if (failure_path.name != "candidate-failure.json" or failure.get("kind") != "completed_without_commit" or
                failure.get("task_id") != task["id"] or failure.get("head_sha") != current or
                failure.get("code_base") != current):
            raise ValueError("编码交接失败证据未绑定本任务当前候选提交。")
        if not any(check["id"] == "ue-task-validation" for check in self.config["checks"]):
            raise ValueError("外层补证恢复需要已配置的任务验证入口。")
        request_path = ue_checks._safe_under(work, Path("Saved/Automation") / task["id"] / "validation-request.json")
        request = ue_checks.validate_request(ue_checks._object(request_path), task["id"], current)
        core = ue_checks.validate_core_tests(task["core_tests"]) if "core_tests" in task else []
        requested = {test["name"] for test in request["tests"] if test["mode"] == "nullrhi"}
        if not set(core) <= requested:
            raise ValueError("外层补证请求必须保留本任务全部核心测试。")
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        recoveries = task.get("external_validation_recoveries", [])
        if any(item.get("head_sha") == current and item.get("request_sha256") == digest for item in recoveries):
            raise ValueError("同一候选提交的相同补证请求已经恢复过。")
        self._claim_session(task, task.get("session_id"))
        task.setdefault("external_validation_recoveries", []).append({
            "head_sha": current, "request_sha256": digest, "review_path": str(review_path),
            "failure_path": str(failure_path), "recovered_at": self.now().isoformat(),
            "previous_check_count": len(task.get("check_results", [])),
        })
        task["check_results"] = []
        state.update(phase="CHECK", operation=None, failures=0, last_error=None)
        state.pop("blocked_from", None)
        state.pop("provider_retry", None)

    def _manual_rework(self, state, *, new_session):
        task = state.get("task")
        if task and task.get("kind") == "art" and state.get("blocked_from") in {"ART_CHECK", "ART_REVIEW"}:
            self._validate_art_task(task, self.runtime / "workspace")
            self._art_rework(state, state.get("last_error") or "用户明确要求继续美术返工。", force=True, new_session=new_session)
            return
        if state.get("blocked_from") != "REVIEW" or not task:
            raise ValueError("人工追加返工只允许从 REVIEW 阻塞恢复。")
        work = self.runtime / "workspace"
        current = gitops.head(work)
        if current != task.get("candidate", {}).get("head_sha") or not gitops.clean(work):
            raise ValueError("人工返工要求当前干净 HEAD 与候选提交一致。")
        directory = self.runtime / "tasks" / task["id"]
        reviews = [(int(p.stem[7:]), p) for p in directory.glob("review-*.json") if p.stem[7:].isdigit()]
        if not reviews:
            raise ValueError("缺少最新验收回执。")
        number, path = max(reviews)
        review = read_json(path)
        if number != task["reworks"] or review.get("decision") not in {"rework", "blocked"} or review.get("head_sha") != current:
            raise ValueError("最新验收必须为 rework 或 blocked，并绑定当前候选 HEAD 和返工轮次。")
        if not isinstance(review.get("feedback"), str) or not isinstance(review.get("summary"), str):
            raise ValueError("最新验收回执缺少反馈。")
        self._claim_session(task, task.get("session_id"))
        task.setdefault("manual_review_recoveries", []).append({
            "original_decision": review["decision"], "review_round": number,
            "head_sha": current, "recovered_at": self.now().isoformat(),
            "new_session": new_session,
        })
        if new_session and task.get("session_id"):
            task.setdefault("session_history", []).append({"session_id": task["session_id"],
                "task_id": task["id"], "retired_at": self.now().isoformat(), "reason": "manual_rework"})
            task["session_id"] = None
            task.pop("session_owner_task_id", None)
        task["feedback"] = (review["feedback"] or review["summary"]) + "\n独立检查：" + json.dumps(task.get("check_results", []), ensure_ascii=False)
        allowance = task.get("manual_rework_allowance", 0)
        if task["reworks"] >= self.config["max_reworks"] + allowance + task.get("autonomous_rework_allowance", 0):
            task["manual_rework_allowance"] = allowance + 1
        task["reworks"] += 1
        task["code_base"], task["code_resume"] = current, False
        task["check_results"] = []
        state.update(phase="CODE", operation=None, failures=0, last_error=None)
        state.pop("blocked_from", None)
        state.pop("provider_retry", None)

    @staticmethod
    def _provider_kind(phase):
        return {"PLAN": "plan", "REPLAN": "plan", "CODE": "code", "REVIEW": "review", "MILESTONE_REVIEW": "review",
                "ART_REVIEW": "review", "ART_DIAGNOSE": "art_diagnose"}.get(phase)

    def _validate_provider_retry(self, state, work):
        retry = state.get("provider_retry")
        if retry is None:
            return None
        task_id = (state.get("task") or {}).get("id")
        phase = state["phase"]
        if (not isinstance(retry, dict) or retry.get("phase") != phase or
                self._provider_kind(phase) is None or retry.get("task_id") != task_id or
                type(retry.get("attempt")) is not int or retry["attempt"] < 1 or
                not isinstance(retry.get("reason"), str) or retry["reason"] not in _PROVIDER_RETRY_REASONS or
                retry.get("status") not in {"waiting", "retrying"} or
                retry.get("head_sha") != gitops.head(work)):
            raise RuntimeError("Provider retry checkpoint differs from the current task, phase, or HEAD.")
        try:
            deadline = datetime.fromisoformat(retry["next_retry_at"])
            if deadline.utcoffset() is None:
                raise ValueError("naive retry deadline")
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Provider retry checkpoint has an invalid deadline.") from error
        op = state.get("operation")
        if retry["reason"] == "codex_subscription_limit" and not (
                phase in {"REVIEW", "MILESTONE_REVIEW", "ART_REVIEW"} or
                phase == "PLAN" and (op or {}).get("planning_provider", self.config.get("planning_provider", "codex")) == "codex"):
            raise RuntimeError("Codex quota wait requires a Codex operation.")
        if op is None:
            if retry["status"] != "retrying" or retry.get("operation_output_dir") is not None:
                raise RuntimeError("Provider retry checkpoint lost its operation binding.")
        elif (op.get("kind") != self._provider_kind(phase) or op.get("task_id") != task_id or
              op.get("output_dir") != retry.get("operation_output_dir")):
            raise RuntimeError("Provider retry checkpoint belongs to another operation.")
        if phase != "CODE" and not gitops.clean(work):
            pending = (state.get("task") or {}).get("replanning") or {}
            if (phase != "REPLAN" or pending.get("source_phase") != "CODE" or
                    pending.get("status") != "pending" or
                    self._replanning_source(state["task"], work) != pending.get("fingerprint")):
                raise RuntimeError("Provider retry requires a clean or fingerprint-bound planning workspace.")
        return deadline

    def _schedule_provider_retry(self, state, phase, error, work):
        op = state.get("operation")
        if not op or op.get("kind") != self._provider_kind(phase):
            raise RuntimeError("Transient provider failure has no matching operation.")
        reason = getattr(error, "retry_reason", None)
        if not isinstance(reason, str) or reason not in _PROVIDER_RETRY_REASONS:
            raise RuntimeError("Transient provider failure has no recognized retry reason.")
        self._validate_operation_task(state)
        op.setdefault("task_id", (state.get("task") or {}).get("id"))
        previous = state.get("provider_retry") or {}
        attempt = previous.get("attempt", 0) + 1
        delay = (180, 300, 600)[min(attempt - 1, 2)]
        requested = getattr(error, "retry_after_seconds", None)
        if reason == "claude_subscription_limit":
            claude_operation = (phase == "ART_DIAGNOSE" or
                                (phase == "CODE" and op.get("coding_provider", self.config.get("coding_provider")) == "claude") or
                                (phase in {"PLAN", "REPLAN"} and op.get("planning_provider", "codex") == "claude"))
            if not claude_operation:
                raise RuntimeError("Claude subscription wait requires a Claude operation.")
            # A Pro reset may be hours or days away. The short service-retry
            # ceiling would spend calls before either quota has recovered.
            delay = requested if type(requested) is int and 0 < requested <= 8 * 86400 else 1800
        elif reason == "codex_subscription_limit":
            if not (phase in {"REVIEW", "MILESTONE_REVIEW", "ART_REVIEW"} or
                    phase == "PLAN" and op.get("planning_provider", self.config.get("planning_provider", "codex")) == "codex"):
                raise RuntimeError("Codex quota wait requires a Codex operation.")
            # The API-first route can recover before the subscription reset
            # after a user replenishes API credits. Recheck at most hourly.
            delay = min(3600, max(60, requested)) if type(requested) is int and requested > 0 else 1800
        elif type(requested) is int and requested > 0:
            delay = min(600, max(180, requested))
        state["provider_retry"] = {
            "phase": phase, "task_id": (state.get("task") or {}).get("id"),
            "head_sha": gitops.head(work), "attempt": attempt,
            "reason": reason,
            "next_retry_at": (self.now() + timedelta(seconds=delay)).isoformat(),
            "operation_output_dir": op["output_dir"], "status": "waiting",
        }
        state["last_error"] = ({"claude_subscription_limit": "Claude Pro 额度已用完，等待自动恢复。",
                                "codex_subscription_limit": "Codex 可用额度不足，等待自动恢复。"}.get(reason) or
                               "Provider temporarily unavailable; waiting for automatic retry.")
        self._save(state)
        snapshot = self._snapshot(state)
        write_json(self.runtime / "heartbeat.json", snapshot)
        return snapshot

    def _wait_seconds(self, result):
        if result.get("status") != "waiting_provider":
            return self.config["poll_seconds"]
        retry = result.get("provider_retry") or {}
        remaining = max(1, (datetime.fromisoformat(retry["next_retry_at"]) - self.now()).total_seconds())
        return max(1, min(60, float(self.config["poll_seconds"]), remaining))

    def _claim_session(self, task, session_id, *, fresh=False):
        """Bind a coding session to one task, including legacy completed receipts."""
        if not session_id:
            return
        if task.get("coding_provider") == "claude" and task.get("session_id") not in (None, session_id):
            raise RuntimeError("Claude Code returned a different session from the active task.")
        if task.get("session_owner_task_id", task["id"]) != task["id"]:
            raise RuntimeError("DSH session owner differs from the current task.")
        path = self.runtime / "dsh-sessions.json"
        owners = read_json(path) if path.exists() else {}
        for receipt in (self.runtime / "tasks").glob("*/*task.json"):
            saved = read_json(receipt)
            entries = [saved] + saved.get("session_history", [])
            for entry in entries:
                identity = entry.get("session_id")
                if identity:
                    owner = saved.get("id", receipt.parent.name)
                    if identity in owners and owners[identity] != owner:
                        raise RuntimeError("Conflicting persisted DSH session ownership.")
                    owners[identity] = owner
        if session_id in owners and owners[session_id] != task["id"]:
            raise RuntimeError("DSH session belongs to another task; refusing cross-task continuation.")
        if fresh and session_id in owners:
            raise RuntimeError("DSH returned a previously used session for a fresh-session request.")
        owners[session_id] = task["id"]
        write_json(path, owners)
        task["session_id"], task["session_owner_task_id"] = session_id, task["id"]

    @staticmethod
    def _validate_operation_task(state):
        op = state.get("operation")
        current = state.get("task") or {}
        if op and "task_id" in op and op["task_id"] != current.get("id"):
            raise RuntimeError("Saved operation belongs to another task.")

    def _timeout(self, maximum):
        if not self._calendar().allowed(self.now()):
            raise gitops.WindowClosed("工作窗口已截止，等待下一个窗口。")
        if self.config["close_policy"] != "strict":
            return float(maximum), False
        remaining = self._calendar().seconds_until_close(self.now())
        if remaining is None:
            return float(maximum), False
        if remaining <= 0:
            raise gitops.WindowClosed("工作窗口已截止。")
        return min(float(maximum), remaining), remaining < maximum

    def _new_operation(self, state, kind):
        op = {"kind": kind, "output_dir": str(self.runtime / "runs" / (kind + "-" + uuid.uuid4().hex)),
              "started_at": self.now().isoformat(), "head_sha": gitops.head(self.runtime / "workspace"),
              "task_id": (state.get("task") or {}).get("id")}
        if kind == "code" and self.config.get("coding_provider") == "claude":
            op.update(coding_provider="claude", coding_model=self.config.get("claude", {}).get("model"),
                      coding_reasoning_effort=self.config.get("claude", {}).get("reasoning_effort"),
                      code_base=(state.get("task") or {}).get("code_base"),
                      requested_session_id=(state.get("task") or {}).get("session_id"))
        if kind == "code" and (state.get("task") or {}).get("kind") == "art":
            op.update(coding_provider="claude", coding_model=state["task"]["coding_model"],
                      coding_reasoning_effort=state["task"]["coding_reasoning_effort"])
        elif kind == "code" and (state.get("task") or {}).get("user_repair"):
            profile = self._active_repair_profile(state["task"])
            op.update(coding_model=profile["model"], coding_reasoning_effort=profile["reasoning_effort"],
                      user_repair_id=profile["id"], user_repair_request_sha256=profile["request_sha256"])
        self._followup_operation(state, op)
        if kind == "art_diagnose":
            op.update(diagnosis_provider="claude", diagnosis_model=state["task"]["coding_model"],
                      diagnosis_reasoning_effort=state["task"]["coding_reasoning_effort"])
        if kind == "plan":
            op["planning_provider"] = self.config.get("planning_provider", "codex")
            selection = ((state.get("task") or {}).get("replanning") if state["phase"] == "REPLAN"
                         else state.get("planning_selection"))
            if selection:
                op.update({key: selection[key] for key in
                           ("planning_model", "planning_reasoning_effort", "planning_level") if key in selection})
            if state["phase"] == "REPLAN":
                op.update(planning_level="critical", replanning_fingerprint=self._replanning_digest(selection["fingerprint"]))
        if state.get("last_error"):
            state.setdefault("operation_error_history", []).append({"error": state["last_error"],
                "failures": state.get("failures", 0), "cleared_at": self.now().isoformat(),
                "next_operation_output_dir": op["output_dir"]})
        state["last_error"] = None
        state["operation"] = op
        if state.get("provider_retry"):
            state["provider_retry"].update(status="retrying", operation_output_dir=op["output_dir"], head_sha=op["head_sha"])
            state["last_error"] = None
        self._save(state)  # Persist intent before starting any process.
        return op

    def _retire_rejected_provider_output(self, state, error):
        """Count a rejected successful CLI receipt once, preserving its evidence.

        Failed/incomplete processes retain their existing recovery rules. Only
        PLAN/REVIEW receipts rejected by the provider validator are retired;
        CODE's session and post-commit reconciliation remain untouched.
        """
        op = state.get("operation")
        if not isinstance(error, ProviderError) or not op or op.get("kind") not in {"plan", "review", "art_diagnose"}:
            return
        directory = codex_result_directory(op["kind"], Path(op["output_dir"]))
        process_path = directory / "process.json"
        try:
            if process_path.is_symlink() or not process_path.is_file() or process_path.stat().st_size > 16384:
                return
            process = read_json(process_path)
            if (process.get("kind") != op["kind"] or type(process.get("exit_code")) is not int or
                    process["exit_code"] != 0 or process.get("timed_out") is not False or
                    process.get("output_limit_exceeded") is not False or process.get("launch_or_capture_failed")):
                return
            rejection = {"kind": op["kind"], "task_id": op.get("task_id"), "head_sha": op["head_sha"],
                         "output_dir": op["output_dir"], "rejected_at": self.now().isoformat(),
                         "reason": "provider_output_validation_failed"}
            audit = directory / "output-rejection.json"
            if not audit.exists():
                write_json(audit, rejection)
        except (OSError, ValueError, TypeError, AttributeError):
            # A receipt that cannot be read or audited keeps the original
            # failure path; never lose the evidence by retiring it blindly.
            return
        state.setdefault("provider_output_failures", []).append(str(audit))
        state["operation"] = None

    def _invoke(self, state, kind, prompt, work, *, providers=None):
        providers = self.providers if providers is None else providers
        self._validate_operation_task(state)
        if kind == "code":
            self._claim_session(state["task"], state["task"].get("session_id"))
        op = state["operation"]
        if op is not None:
            if op["kind"] != kind:
                raise RuntimeError("断点步骤与状态不一致。")
            if kind in {"plan", "review", "art_diagnose"} and op["head_sha"] != gitops.head(work):
                raise RuntimeError("审阅或规划期间候选提交被改变。")
            recovered = providers.recover(kind, Path(op["output_dir"]))
            if recovered is not None:
                if kind == "code":
                    self._claim_session(state["task"], recovered.get("session_id"))
                return recovered
            # A supervisor crash can leave the failed process receipt before
            # its retry deadline was saved. Classify it read-only on recovery.
            retry = state.get("provider_retry") or {}
            if not (retry.get("status") == "waiting" and retry.get("operation_output_dir") == op["output_dir"]):
                from .providers import classify_transient_failure
                transient = classify_transient_failure(kind, Path(op["output_dir"]))
                if transient:
                    raise ProviderError("Saved provider attempt failed transiently", transient=True,
                                        retry_reason=transient["reason"], retry_after_seconds=transient.get("retry_after_seconds"),
                                        session_id=read_session(Path(op["output_dir"])) if kind == "code" else None)
            if kind == "code":
                session = read_session(Path(op["output_dir"]))
                if session:
                    self._claim_session(state["task"], session)
                state["task"]["code_resume"] = True
            # The old invocation is never replayed into the same log directory.
            state["operation"] = None
            if state.get("provider_retry"):
                state["provider_retry"].update(status="retrying", operation_output_dir=None)
                state["last_error"] = None
            self._save(state)
        op = self._new_operation(state, kind)
        maximum = min(900, self.config["step_timeout_seconds"]) if kind == "art_diagnose" else self.config["step_timeout_seconds"]
        if kind == "code" and (state.get("task") or {}).get("kind") != "art":
            maximum = self.config.get("code_timeout_seconds", maximum)
        timeout, window_limited = self._timeout(maximum)
        op["window_limited"] = window_limited
        op["timeout_seconds"] = timeout
        self._save(state)
        if kind == "code":
            requested_session = state["task"].get("session_id")
            result = providers.code(prompt, work, Path(op["output_dir"]), timeout,
                                         session_id=requested_session)
            self._claim_session(state["task"], result.get("session_id"), fresh=requested_session is None)
            return result
        if kind == "art_diagnose":
            return providers.art_diagnose(prompt, work, Path(op["output_dir"]), timeout,
                                          session_id=state["task"].get("session_id"))
        if kind == "plan" and op.get("planning_model"):
            return providers.plan(prompt, work, Path(op["output_dir"]), timeout,
                                       model=op["planning_model"], reasoning_effort=op["planning_reasoning_effort"],
                                       tiered=state["phase"] != "REPLAN")
        return getattr(providers, kind)(prompt, work, Path(op["output_dir"]), timeout)

    def _base_prompt(self):
        prompt = ("你参与一个已授权的单机、单项目、串行开发闭环。请先读取仓库 AGENTS.md、项目文档和真实 Git 状态。\n"
                "总体开发目标：\n" + self.config["goal"] + "\n"
                "只依据实际代码、命令、日志和证据作结论；缺少必要信息或运行证据不得宣布完成或验收通过。\n")
        if self.config.get("planning_provider", "codex") == "claude":
            coder = "Claude Code（CC）" if self.config.get("coding_provider") == "claude" else "DSH"
            prompt += (f"用户当前分工：Claude Code（CC）规划任务，{coder}负责编码和本地提交，Codex 负责独立任务验收和阶段验收。\n")
            if self.config["claude"].get("critical_plan_model"):
                prompt += "用户规划分工：Sonnet 规划常规任务并判断实际改动范围；关键系统任务和阶段完成提案由 Opus 完成最终规划，编码使用 Sonnet。\n"
        elif self.config.get("coding_provider") == "claude":
            prompt += "用户当前分工：Codex 只发布任务书与独立验收，Claude Code（CC）负责编码和本地提交。\n"
        if self.config.get("coding_provider") == "claude":
            prompt += "历史任务书、反馈和文档中的 DSH 开发职责由 CC 接续。\n"
        publication = self._publication()
        if publication:
            prompt += (f"当前用户最新发布策略：只完成里程碑 {publication['milestone_id']}，其完整边界与完成条件如下：\n"
                       + publication["goal"] + "\n"
                       "每个任务只做本地 commit。里程碑通过完整独立检查与 Codex 系统审核后集中 push，人工待确认事项单独保留。\n"
                       "系统发布与人工验收分别记录。旧任务书或文档中的发布时机以本策略为准。\n"
                       "规划者可在系统工作完成后提交 complete 提案并保留人工待办。系统发布前仍需最终独立检查与 Codex 系统审核。\n"
                       "不得自行开始下一里程碑；不得把某个修复或几个定向测试通过当作整个阶段完成。\n")
        return prompt

    def _plan(self, state, work):
        if not gitops.clean(work):
            raise RuntimeError("规划前工作区存在未提交内容。")
        if self._dispatch_art(state, work) or state["phase"] == "DONE":
            return
        current = gitops.head(work)
        task_id = f"{self.config['task_prefix']}-{state['sequence']:04d}"
        selection = state.get("planning_selection")
        claude = self.config.get("claude", {})
        tiered = self.config.get("planning_provider", "codex") == "claude" and bool(claude.get("critical_plan_model"))
        if selection is None and tiered:
            selection = {
                "head_sha": current, "planning_model": claude["model"],
                "planning_reasoning_effort": claude.get("reasoning_effort", "high"), "planning_level": "routine",
                "critical_plan_model": claude["critical_plan_model"],
                "critical_plan_reasoning_effort": claude.get("critical_plan_reasoning_effort", claude.get("reasoning_effort", "high")),
            }
            state["planning_selection"] = selection
            self._save(state)
        if selection is not None and (
                not isinstance(selection, dict) or selection.get("head_sha") != current or
                selection.get("planning_level") not in {"routine", "critical"} or
                self.config.get("planning_provider", "codex") != "claude"):
            raise RuntimeError("规划模型断点与当前提供方或 HEAD 不匹配。")
        planned_tests = (self.config.get("coding_provider") == "claude" and
                         self.config["claude"].get("planned_core_tests") is True)
        publication = self._publication()
        completion_rule = ("里程碑系统工作完成后返回 action=complete 提交阶段提案，人工待办可继续保留。\n"
                           if publication else "全部目标完成后返回 action=complete。\n")
        planner = "Claude Code" if self.config.get("planning_provider", "codex") == "claude" else "Codex"
        prompt = self._base_prompt() + (
            f"你是 {planner} 项目经理。当前固定 HEAD={current}；下一任务编号 {task_id}。\n"
            "本轮只规划并发布一个有限、可执行、有明确验收条件的任务，不修改代码、不提交、不推送。\n"
            + completion_rule + "缺必要系统决策返回 blocked，其余待实现工作返回 task。\n"
            "验收条件须覆盖真实行为，涉及界面/游戏画面须要求实际运行证据。\n"
            "已通过任务记录：\n" + json.dumps(state["accepted"][-20:], ensure_ascii=False) + "\n"
            "用户明确延期、仍待人工确认的任务（不是验收通过）：\n" + json.dumps(state.get("deferred", []), ensure_ascii=False) + "\n"
            "保留这些人工待办及其状态，由用户后续确认。规划继续其他可自动化开发事项。提案须列明仍存在的系统缺项。\n"
            "已由独立美术任务处理、等待用户视觉确认的资产（不重新派发、不阻塞其他开发）：\n" +
            json.dumps([{key: item.get(key) for key in ("id", "title", "review_package", "review_package_sha256")} for item in state.get("art_pending", [])], ensure_ascii=False) + "\n"
            "阶段复核尚待解决的反馈：\n" + state.get("milestone_feedback", ""))
        if planned_tests:
            prompt += (
                "\n任务书明确写出玩家能完成的流程、本次功能范围和明确留到后续的事项。"
                "验收条件写明场景、操作与可观察的预期结果，覆盖本轮核心功能。"
                "为 action=task 返回 core_tests，列 1 至 5 个可在 NullRHI 执行的完整 ZhuGuoCity 自动化测试名。"
                "core_tests 每个字符串只能是完整测试名，不得附加括号、验收编号、说明或命令。"
                "在任务书正文另行注明各测试对应的验收条件，以及是复用还是由 CC 新增。"
                "CC 提交前跑这组测试，提交后独立复跑。"
                "全量自动回归由里程碑检查执行。action=complete 或 blocked 时 core_tests 返回空列表。\n"
            )
        if selection is not None:
            prompt += (
                "\n按本轮计划实际修改的接口、数据流和行为范围返回 planning_level=routine 或 critical，"
                "并用 planning_reason 说明判断依据。关键系统包括存档与兼容迁移、经济与资源结算、"
                "跨系统状态一致性、调度与寻路、架构或公共接口等影响多个模块正确性的改动。"
                "局部界面、文案、样式和范围明确的局部修复通常为 routine。判断依据须对应具体改动，"
                "历史文档出现关键系统名称本身不足以提高等级。\n"
            )
            if selection["planning_level"] == "critical":
                prompt += (
                    "本轮由 Opus 完成最终规划。下面的 Sonnet 候选任务是待核实的提案。"
                    "请重新读取实际源码和证据，核对关键系统边界、兼容性与验收条件后发布最终任务书。"
                    "返回 planning_level=critical。阶段完成提案必须逐项核对系统完成条件，并列出保留的人工待办。\n"
                    "Sonnet 候选提案：\n" + json.dumps(selection["critical_candidate"], ensure_ascii=False) + "\n"
                )
        result = self._invoke(state, "plan", prompt, work)
        if gitops.head(work) != current or not gitops.clean(work):
            raise RuntimeError("规划改变了候选代码。")
        operation = state["operation"]
        planning = {key: operation[key] for key in
                    ("planning_model", "planning_reasoning_effort", "planning_level") if key in operation}
        if planning:
            if (result.get("planning_level") not in {"routine", "critical"} or
                    not isinstance(result.get("planning_reason"), str) or not result["planning_reason"].strip()):
                raise ProviderError("分级规划需要有效的 planning_level 和 planning_reason。")
            planning["planning_reason"] = result["planning_reason"]
            write_json(Path(operation["output_dir"]) / "planning-decision.json", {
                "head_sha": current, **planning, "result": result,
                "source_output_dir": selection.get("source_output_dir"),
            })
            if selection["planning_level"] == "routine" and (
                    result["planning_level"] == "critical" or result["action"] == "complete"):
                state["planning_selection"] = {
                    **selection, "planning_level": "critical", "planning_model": selection["critical_plan_model"],
                    "planning_reasoning_effort": selection["critical_plan_reasoning_effort"],
                    "critical_candidate": result, "source_output_dir": operation["output_dir"],
                }
                state["operation"] = None
                self._save(state)
                return
        core_tests = None
        if planned_tests and result["action"] == "task":
            from .ue_checks import ValidationError, validate_core_tests
            try:
                core_tests = validate_core_tests(result.get("core_tests"))
            except ValidationError as error:
                raise ProviderError("任务书需要有效的核心测试清单。") from error
        state["operation"] = None
        state.pop("planning_selection", None)
        if result["action"] == "complete":
            pending = self._pending_milestone_tasks(state) if publication else []
            deferred = self._deferred_tasks(state)
            if publication and (pending or deferred):
                carried = sorted(pending + deferred, key=lambda record: int(record["id"].rsplit("-", 1)[-1]))
                previous = state.get("milestone") or {}
                same_head_reworks = (previous.get("same_head_reworks", 0) + 1
                                     if previous.get("status") == "rework" and previous.get("head_sha") == current else 0)
                if same_head_reworks > self.config["max_reworks"]:
                    self._block(state, "PLAN", "阶段复核要求的修正尚未产生新提交，同一 HEAD 的重复完成提案已达到上限。")
                    return
                state["milestone"] = {
                    "id": publication["milestone_id"], "goal": publication["goal"],
                    "head_sha": current, "base_sha": carried[0]["base_sha"],
                    "task_ids": [record["id"] for record in pending], "accepted_tasks": pending,
                    "deferred_tasks": deepcopy(deferred),
                    "proposal": result, "check_results": [], "review": None,
                    "attempt": previous.get("attempt", 0) + 1, "same_head_reworks": same_head_reworks,
                }
                if self.config.get("milestone_checks"):
                    state["milestone"]["milestone_checks"] = deepcopy(self.config["milestone_checks"])
                state["phase"] = "MILESTONE_REVIEW"
                return
            if any(record.get("status") == "pending_manual_confirmation" for record in deferred):
                self._block(state, "PLAN", "仍有用户延期且待人工确认的任务；不能凭延期宣布全部目标完成。")
                return
            state["phase"] = "DONE"
            state["completion_reason"] = result["reason"]
            return
        if result["action"] == "blocked":
            self._block(state, "PLAN", result["reason"])
            return
        task = {"id": task_id, "title": result["title"], "body": result["body"],
                "acceptance_criteria": result["acceptance_criteria"], "base_sha": current,
                "code_base": current, "reworks": 0, "session_id": None, "feedback": "",
                "code_resume": False}
        task.update(planning)
        if planning and selection.get("source_output_dir"):
            task["planning_source_output_dir"] = selection["source_output_dir"]
        if self.config.get("coding_provider") == "claude":
            task["coding_provider"] = "claude"
        if core_tests is not None:
            task["core_tests"] = core_tests
        title = result["title"] if result["title"].startswith(task_id) else task_id + " " + result["title"]
        directory = self.runtime / "tasks" / task_id
        write_json(directory / "task.json", task)
        task_book = (title + "\n\nBase SHA: " + current + "\n\n" + task["body"] +
                     "\n\n验收条件：\n" + "\n".join("- " + item for item in task["acceptance_criteria"]) + "\n")
        if core_tests is not None:
            task_book += "\n提交前核心测试（NullRHI）\n" + "\n".join("- " + name for name in core_tests) + "\n"
        write_text(directory / "TASK.md", task_book)
        state["task"], state["phase"] = task, "CODE"

    @staticmethod
    def _art_head(task):
        return task.get("art_workspace_head", task["base_sha"])

    def _art_recipe(self, task, work):
        from .art_pipeline import load_config, recipe_file_path
        cfg = load_config(task["art_request"]["config_file"])
        path = recipe_file_path(cfg, workspace=work)
        return cfg, path, path.relative_to(Path(work).resolve()).as_posix() if path else None

    def _art_files(self, task, work):
        from .art_pipeline import blender_job_file_path
        cfg, recipe, relative = self._art_recipe(task, work)
        paths = {relative: recipe} if recipe else {}
        job = blender_job_file_path(cfg, workspace=work)
        if job is not None:
            paths[job.relative_to(Path(work).resolve()).as_posix()] = job
        return cfg, paths

    @staticmethod
    def _art_changes(work):
        changes = set(gitops.git(work, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0")) - {""}
        changes.update(set(gitops.git(work, "ls-files", "--others", "-z").split("\0")) - {""})
        return changes

    @staticmethod
    def _art_commit_scope(intent, paths):
        """Normalize both the historical recipe intent and exact-file v2 intents."""
        if intent.get("version") is None:
            relative = intent.get("relative_path")
            if len(paths) != 1 or relative not in paths or not relative.endswith(".json"):
                raise RuntimeError("旧美术配方提交意图不能绑定新的编辑范围。")
            files, changed = {relative: intent.get("recipe_sha256")}, [relative]
        else:
            files, changed = intent.get("files"), intent.get("changed_paths")
            if (type(intent.get("version")) is not int or intent["version"] != 2 or
                    not isinstance(files, dict) or set(files) != set(paths) or
                    not isinstance(changed, list) or not changed or
                    any(not isinstance(name, str) or name not in paths for name in changed) or
                    changed != sorted(set(changed))):
                raise RuntimeError("美术提交意图的精确文件范围无效。")
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in files.values()):
            raise RuntimeError("美术提交意图的文件摘要无效。")
        return files, set(changed)

    def _validate_art_task(self, task, work, *, allow_recipe_dirty=False):
        settings = self.config.get("art_pipeline")
        request = task.get("art_request", {})
        if (not settings or task["id"] not in settings["task_ids"] or
                request.get("config_file") != settings["config_file"] or
                request.get("artifacts_dir") != settings["artifacts_dir"]):
            raise RuntimeError("美术任务的固定执行入口或授权范围改变。")
        if hashlib.sha256(Path(request["config_file"]).read_bytes()).hexdigest() != request["config_sha256"]:
            raise RuntimeError("已入队的美术批次配置已改变，需要按新版本重新派发。")
        if gitops.head(work) != self._art_head(task):
            raise RuntimeError("美术工作副本 HEAD 偏离任务检查点，不能继续验收。")
        _, paths = self._art_files(task, work)
        if not paths:
            if gitops.clean(work):
                return
            raise RuntimeError("美术工作副本存在未授权变化。")
        changes = self._art_changes(work)
        if not changes or allow_recipe_dirty and changes <= set(paths):
            return
        raise RuntimeError("美术工作副本只能修改可信配置指定的 recipe_file 和 blender_job_file，存在其他变化或未跟踪文件。")

    def _recover_art_recipe_commit(self, state, work):
        """Adopt only the exact controller-intended commit after a crash."""
        task = state.get("task") or {}
        intent = task.get("art_recipe_commit")
        if not intent:
            return False
        request = task["art_request"]
        if (intent["base_sha"] != self._art_head(task) or not state.get("operation") or
                state["operation"].get("kind") != "code" or state["operation"].get("task_id") != task["id"] or
                state["operation"].get("head_sha") != intent["base_sha"] or
                intent["operation_output_dir"] != state["operation"]["output_dir"] or
                hashlib.sha256(Path(request["config_file"]).read_bytes()).hexdigest() != request["config_sha256"]):
            raise RuntimeError("美术配方提交意图与任务、配置或操作断点不匹配。")
        current = gitops.head(work)
        _, paths = self._art_files(task, work)
        files, changed = self._art_commit_scope(intent, paths)
        package = Path(task["art_request"]["artifacts_dir"]) / "review-package.json"
        if current == intent["base_sha"]:
            if (any(hashlib.sha256(paths[name].read_bytes()).hexdigest() != digest for name, digest in files.items()) or
                    hashlib.sha256(package.read_bytes()).hexdigest() != intent["review_package_sha256"]):
                raise RuntimeError("待提交美术文件或验收包偏离已持久化意图。")
            return False
        changes = set(gitops.git(work, "diff", "--name-only", "--no-renames", "-z", intent["base_sha"], current).split("\0")) - {""}
        if (self._art_changes(work) or changes != changed or
                gitops.git(work, "rev-list", "--count", intent["base_sha"] + ".." + current) != "1" or
                gitops.git(work, "rev-parse", current + "^") != intent["base_sha"] or
                gitops.git(work, "show", "-s", "--format=%s", current) != task["id"] or
                any(hashlib.sha256(paths[name].read_bytes()).hexdigest() != digest for name, digest in files.items()) or
                hashlib.sha256(package.read_bytes()).hexdigest() != intent["review_package_sha256"]):
            raise RuntimeError("美术配方提交与控制器持久意图不匹配，不能接管。")
        record = {**intent, "head_sha": current, "committed_at": self.now().isoformat()}
        task.setdefault("art_recipe_commits", []).append(record)
        task.pop("art_recipe_commit")
        task.update(art_workspace_head=current, code_base=current, candidate={"head_sha": current})
        state["expected_head"] = current
        if state.get("operation"):
            state["operation"].setdefault("original_head_sha", state["operation"].get("head_sha"))
            state["operation"]["head_sha"] = current
        self._save(state)
        return True

    def _commit_art_recipe(self, state, work):
        task = state["task"]
        self._validate_art_task(task, work, allow_recipe_dirty=True)
        if gitops.clean(work):
            return
        from .art_pipeline import blender_job_snapshot, recipe_overlay, verify_review
        cfg, paths = self._art_files(task, work)
        _, recipe, _ = recipe_overlay(cfg)
        job, _ = blender_job_snapshot(cfg)
        provenances = {Path(value["source_file"]): value["sha256"] for value in (recipe, job) if value is not None}
        files = {name: provenances[path] for name, path in paths.items()}
        changed = sorted(self._art_changes(work))
        checked = verify_review(cfg, persist=False)
        intent = {"version": 2, "base_sha": self._art_head(task), "files": files, "changed_paths": changed,
                  "review_package_sha256": checked["review_package_sha256"],
                  "operation_output_dir": state["operation"]["output_dir"]}
        existing = task.get("art_recipe_commit")
        if existing is not None:
            old_files, old_changed = self._art_commit_scope(existing, paths)
            if (old_files != files or old_changed != set(changed) or
                    any(existing.get(key) != intent[key] for key in ("base_sha", "review_package_sha256", "operation_output_dir"))):
                raise RuntimeError("待提交美术文件或验收包改变，不能覆盖原提交意图。")
            intent = existing
        task["art_recipe_commit"] = intent
        self._save(state)
        self._validate_art_task(task, work, allow_recipe_dirty=True)
        if (self._art_changes(work) != set(changed) or
                any(hashlib.sha256(paths[name].read_bytes()).hexdigest() != digest for name, digest in files.items()) or
                hashlib.sha256((Path(task["art_request"]["artifacts_dir"]) / "review-package.json").read_bytes()).hexdigest()
                != intent["review_package_sha256"]):
            raise RuntimeError("美术文件或验收包在提交前改变。")
        gitops.git(work, "add", "--", *changed)
        gitops.git(work, "commit", "--only", "-m", task["id"], "--", *changed, timeout=120)
        if not self._recover_art_recipe_commit(state, work):
            raise RuntimeError("控制器未产生可验证的配方提交。")

    def _art_providers(self, task):
        if self._providers_injected:
            return self.providers
        config = deepcopy(self.config)
        config["coding_provider"] = "claude"
        settings = config.setdefault("claude", {})
        settings.update(model=task["coding_model"], reasoning_effort=task["coding_reasoning_effort"],
                        ue_preflight=False, planned_core_tests=False)
        for key in ("instructions", "test_preflight", "visual_preflight"):
            settings.pop(key, None)
        return AgentProviders(config)

    def _refresh_art_evidence(self, request, evidence):
        """Recheck current bytes/budget without rewriting the reviewed receipt."""
        from .art_pipeline import load_config, verify_review
        if hashlib.sha256(Path(request["config_file"]).read_bytes()).hexdigest() != request["config_sha256"]:
            raise RuntimeError("美术验收使用的批次配置已改变。")
        for path_key, hash_key in (("review_package", "review_package_sha256"), ("independent_check", "independent_check_sha256")):
            if hashlib.sha256(Path(evidence[path_key]).read_bytes()).hexdigest() != evidence[hash_key]:
                raise RuntimeError("美术验收证据在审核前改变。")
        result = verify_review(load_config(request["config_file"]), persist=False)
        if result["review_package_sha256"] != evidence["review_package_sha256"]:
            raise RuntimeError("美术验收包在重新核验期间改变。")

    def _art_code(self, state, work):
        task = state["task"]
        self._recover_art_recipe_commit(state, work)
        self._validate_art_task(task, work, allow_recipe_dirty=True)
        prompt = (
            "你是统一无人值守开发系统中的美术资产制作执行者。本任务使用 Claude Opus 5.5、high 思考。\n"
            "按照已确认的风格参考和本轮任务书，调用调度器追加的固定美术 preview 入口。"
            "具体制作方法和实现选择由你自主决定，维护方不代替你分析或造型。"
            "在用户已确认风格、范围、预算、工具与验收边界内，自主诊断问题、调整实现和重跑。"
            "只有必须改变这些授权边界或缺少不可获得的外部条件时才请求用户决策；"
            "不要把自己以前的自主选型或维护方的历史尝试自动视为用户硬要求。"
            "恢复时复用已有 Tripo 任务、源模型和本地处理缓存；不得重复付费提交未知任务。"
            "本任务在外部资产目录生产，不修改主游戏工作副本、不 git commit、不 push。"
            "调用完成后读取实际验收包和展示图，说明已完成步骤、费用和待用户视觉确认项。"
            "不能把文件存在、命令退出零或模型文字声明当作美术验收通过。"
            "不得超出三个预览等级、3000积分批次预算或扩大白名单命令。\n"
            "只可编辑可信 art_request.config_file 指定的精确 recipe_file 和 blender_job_file；由控制器核验并提交。"
            "若指定 blender_job_file，你须作为真实美术作者自行编写该 Blender 脚本，先读取工作区只读 BLENDER_JOB_API.md。"
            "脚本实现 run_job(context)，返回 {'instances':list,'plots':list,'report':dict}；"
            "context 提供 houses、entries、manifest、output_dir、assets、materials 和固定 api。"
            "由你修改房屋、制作院落及必要生活道具，并通过 export_asset 重新导出和保留来源记录；"
            "配置脚本时固定院落/屋顶预设会跳过，不得把维护验证稿冒充你的正式作品。"
            "只经固定 preview 运行；禁止改框架、可信配置、账本、预算、主游戏或新增命令权限。"
            "若可信配置指定 recipe_file，先读取该路径；"
            "不得修改可信批次配置。recipe 必须保留批准顺序的全部三级资产：顶层仅 version=1 和 assets，"
            "每条仅 asset_id、revision、prompt、target_height_m、max_footprint_m、front_rotation_deg。"
            "不得加入或修改 level、预算、参考与资产编号。prompt 改动必须只为需要重做的级别递增 vNNN revision；"
            "仅尺寸或朝向处理可保留 revision 复用模型。修改须处于入口验证的边界内，再调用同一固定 preview。"
            "没有 recipe_file 时不得创建自定配方或绕过白名单。\n"
            "任务书与当前反馈：\n" + json.dumps(task, ensure_ascii=False))
        result = self._invoke(state, "code", prompt, work, providers=self._art_providers(task))
        if result.get("completed") is not True:
            raise RuntimeError("Claude 美术制作回合未完成。")
        self._commit_art_recipe(state, work)
        self._validate_art_task(task, work)
        self._delivery(task, result.get("text", ""))
        if isinstance(result.get("model_selection"), dict):
            task["model_selection"] = deepcopy(result["model_selection"])
        task["check_results"] = []
        task["candidate"] = {"head_sha": gitops.head(work)}
        state.update(operation=None, phase="ART_CHECK")

    def _art_rework(self, state, feedback, *, force=False, new_session=False):
        task = state["task"]
        if not force and task["reworks"] >= (self.config["max_reworks"] + task.get("manual_rework_allowance", 0)
                                            + task.get("autonomous_rework_allowance", 0)):
            if self._begin_art_diagnosis(state, feedback, trigger="rework_limit"):
                return
            self._block(state, state["phase"], "美术任务已达到自动返工次数上限。" + feedback)
            return
        task.setdefault("art_attempts", []).append({"rework": task["reworks"], "feedback": feedback,
                                                   "check_results": deepcopy(task.get("check_results", [])),
                                                   "art_review": deepcopy(task.get("art_review")), "at": self.now().isoformat()})
        if force:
            task["manual_rework_allowance"] = task.get("manual_rework_allowance", 0) + 1
            if self._art_autonomy_policy()["enabled"]:
                task["art_autonomy_epoch"] = task.get("art_autonomy_epoch", 0) + 1
        if new_session and task.get("session_id"):
            task.setdefault("session_history", []).append({"session_id": task["session_id"], "task_id": task["id"],
                                                          "retired_at": self.now().isoformat(), "reason": "manual_art_rework"})
            task["session_id"] = None
            task.pop("session_owner_task_id", None)
        task.update(reworks=task["reworks"] + 1, feedback=feedback, code_resume=True, check_results=[])
        task.pop("art_review", None)
        state.update(phase="CODE", operation=None, failures=0, last_error=None)
        state.pop("blocked_from", None)
        state.pop("provider_retry", None)

    def _art_check(self, state, work):
        task = state["task"]
        self._validate_art_task(task, work)
        request = task["art_request"]
        check = {"id": "art-pipeline-verify", "timeout_seconds": 600,
                 "argv": [sys.executable, str(Path(__file__).with_name("art_pipeline.py").resolve()),
                          "--config", request["config_file"], "verify"],
                 "success_patterns": ['"status": "passed"'], "failure_patterns": []}
        result = self._independent_check(state, work, self._art_head(task), check, "art_check")
        if result is None:
            return
        task["check_results"] = [result]
        state["operation"] = None
        if not result["passed"]:
            self._art_rework(state, "美术独立检查失败；依据本次原始检查日志处理，不得跳过验证。\n" + json.dumps(result, ensure_ascii=False))
            return
        root = Path(request["artifacts_dir"])
        package = root / "review-package.json"
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        receipt = read_json(root / "independent-check.json")
        if receipt.get("status") != "passed" or receipt.get("review_package_sha256") != digest:
            raise RuntimeError("美术检查回执没有绑定当前验收包。")
        task["art_review"] = {"review_package": str(package), "review_package_sha256": digest,
                              "independent_check": str(root / "independent-check.json"),
                              "independent_check_sha256": hashlib.sha256((root / "independent-check.json").read_bytes()).hexdigest()}
        state["phase"] = "ART_REVIEW"

    def _art_review(self, state, work):
        task = state["task"]
        self._validate_art_task(task, work)
        evidence = task["art_review"]
        self._refresh_art_evidence(task["art_request"], evidence)
        expected_head = self._art_head(task)
        prompt = (
            "你是统一无人值守系统的 Codex 独立美术技术验收者，只审阅、不修改文件或发布。\n"
            f"固定美术工作副本 HEAD={expected_head}，资产验收包 SHA256={evidence['review_package_sha256']}。\n"
            "读取验收包、独立检查、实际展示截图与任务书。核对三级范围、参考版本、预算、"
            "尺度、材质和院落展示，明确可观察的视觉缺陷与证据限制。"
            "技术与展示证据足够可返回 accepted；需要修复返回 rework；缺少不可获得的条件才返回 blocked。"
            "你的 accepted 只代表可提交用户视觉验收，不代表用户已经批准，不得释放其余五级。"
            "返回既定 review JSON，head_sha 必须为本条指定的美术工作副本 HEAD，不能使用历史 base_sha。\n"
            + json.dumps(task, ensure_ascii=False))
        result = validate_output("review", self._invoke(state, "review", prompt, work, providers=self._art_providers(task)))
        self._validate_art_task(task, work)
        self._refresh_art_evidence(task["art_request"], evidence)
        if result["head_sha"] != expected_head:
            raise RuntimeError("美术审阅没有绑定当前项目和验收包。")
        write_json(self.runtime / "tasks" / task["id"] / f"review-{task['reworks']}.json", result)
        if result["decision"] == "blocked":
            if self._begin_art_diagnosis(state, result["feedback"] or result["summary"], trigger="review_blocked"):
                return
            state["operation"] = None
            self._block(state, "ART_REVIEW", result["feedback"] or result["summary"])
        elif result["decision"] == "rework":
            self._art_rework(state, result["feedback"] or result["summary"])
        else:
            record = {"id": task["id"], "title": task["title"], "status": "awaiting_approval",
                      "head_sha": expected_head, **deepcopy(evidence), "technical_review": result,
                      "submitted_at": self.now().isoformat(), "task": deepcopy(task)}
            write_json(self.runtime / "tasks" / task["id"] / "art-pending.json", record)
            with lock(self.runtime / "art-queue.lock"):
                queue = self.art_queue()
                item = next(entry for entry in queue["requests"] if entry["id"] == task["id"])
                item.pop("decision", None)
                item.update(status="awaiting_approval", **evidence)
                state.setdefault("art_pending", []).append(record)
                state.update(task=None, phase="PLAN", operation=None)
                self._save(state)
                write_json(self.runtime / "art-queue.json", queue)

    def _code(self, state, work):
        task = state["task"]
        if task.get("kind") == "art":
            return self._art_code(state, work)
        if (task.get("user_followup") or {}).get("status") in {"active", "exhausted"}:
            return self._user_followup_code(state, work)
        providers = self._user_repair_providers(task)
        coder = self.config.get("coding_provider", "dsh")
        if task.get("coding_provider", "dsh") != coder:
            raise RuntimeError("任务的编码会话需要先完成提供方交接。")
        coder_name = "Claude Code" if coder == "claude" else "DSH"
        self._validate_operation_task(state)
        self._claim_session(task, task.get("session_id"))
        current = gitops.head(work)
        if state["operation"] and current != task["code_base"] and gitops.clean(work):
            # Crash after commit, before the provider or state flush. Reconcile
            # actual Git first, so a restart cannot create a duplicate commit.
            task["candidate"] = gitops.verify_candidate(work, task["base_sha"], task["id"])
            task["candidate"]["recovered_from_git"] = True
            op = state["operation"]
            session = read_session(Path(op["output_dir"]))
            if session:
                self._claim_session(task, session)
            recovered = providers.recover("code", Path(op["output_dir"]))
            if recovered:
                self._claim_session(task, recovered.get("session_id"))
            report = recovered.get("text", "") if recovered else f"程序中断后从实际 Git 提交恢复。缺少完整 {coder_name} 完成回执，须由独立检查和 Codex 核对交付完整性。"
            self._delivery(task, report)
            task["check_results"] = []
            state["operation"], state["phase"] = None, "CHECK"
            return
        if not state["operation"] and not task.get("code_resume") and (current != task["code_base"] or not gitops.clean(work)):
            raise RuntimeError("编码开始前工作区被外部改变。")
        prompt = self._base_prompt() + (
            f"你是 {coder_name} 开发者。按以下任务书编码、运行必要测试并自行 git commit。\n"
            "每个 commit 标题必须仅为任务编号；只提交本任务文件，不强推、不重写历史、不更改分支。\n"
            "不要 git push，调度器在 Codex 验收通过后按配置推送工作分支。\n"
            "这是可恢复步骤：先检查当前 Git、已有提交和未提交修改，继续尚未完成的工作，禁止重复创建已存在提交。\n"
            "如果代码已完成提交，核对证据并交付；不要为产生新提交而改文件。\n"
            "任务书：\n" + json.dumps(task, ensure_ascii=False) + "\n"
            "交付报告需说明实际测试结果及最终 HEAD；不要把未运行项目写成通过。")
        if coder == "claude":
            prompt += (
                "\n当前工作目录已经是任务仓库。Git 请逐条调用，例如 git status --short、git diff --check，"
                "不加 git -C，不用 cd、管道或复合命令。"
                "构建和测试使用前台 Bash，等待本条命令结束再执行下一条。\n")
        if task.get("code_resume") and task.get("code_failures"):
            prompt += (
                f"\n执行器交付检查反馈：上次 {coder_name} 协议轮已结束，但相对 "
                f"code_base={task['code_base']} 没有产生新的任务 commit，实际任务交付尚未完成。\n"
                "继续当前任务、原验收/返工反馈及现有未提交修改，先核对真实 Git 状态并完成尚缺实现；"
                "不能仅回复计划，也不得为满足提交条件制造无意义改动。\n"
                "将本任务实际完成的修改以任务编号为唯一 commit 标题提交，核对必要验证与证据，"
                "然后交付最终完整 HEAD；保留已有工作，不重复已有提交、不改写原验收反馈。\n")
        result = self._invoke(state, "code", prompt, work, providers=providers)
        task["session_id"] = result.get("session_id", task.get("session_id"))
        if result.get("completed") is not True:
            raise RuntimeError(f"{coder_name} 未完成本轮。")
        observed_head = gitops.head(work)
        if observed_head == task["code_base"]:
            # A completed protocol turn is not a completed code delivery. Retire
            # this receipt before retrying, otherwise recover() returns the same
            # no-commit result forever without invoking DSH again.
            op = state["operation"]
            failure_path = Path(op["output_dir"]) / "candidate-failure.json"
            write_json(failure_path, {"kind": "completed_without_commit", "task_id": task["id"],
                       "observed_at": self.now().isoformat(), "head_sha": observed_head,
                       "code_base": task["code_base"], "session_id": task.get("session_id"),
                       "operation": dict(op), "provider_result": result})
            task.setdefault("code_failures", []).append(str(failure_path))
            task["code_resume"] = True
            state["operation"] = None
            self._save(state)
            raise RuntimeError(f"{coder_name} 报告完成，但没有产生新的任务提交。")
        task["candidate"] = gitops.verify_candidate(work, task["base_sha"], task["id"])
        task["check_results"] = []
        self._delivery(task, result.get("text", ""))
        state["operation"], state["phase"] = None, "CHECK"

    def _delivery(self, task, text):
        path = self.runtime / "tasks" / task["id"] / f"delivery-{task['reworks']}.md"
        write_text(path, text)
        task["delivery_path"], task["delivery_text"] = str(path), text

    def _check(self, state, work):
        task = state["task"]
        if self._consume_user_followup(state, work):
            return
        if not self.config["checks"]:
            self._block(state, "CHECK", "尚未配置独立验收命令 checks；请配置真实项目检查。")
            return
        if gitops.head(work) != task["candidate"]["head_sha"] or not gitops.clean(work):
            raise RuntimeError("独立检查前候选提交或工作区改变。")
        results = task["check_results"]
        checks = self._task_checks(task)
        if len(results) > len(checks) or any(row.get("id") != checks[index]["id"] for index, row in enumerate(results)):
            raise RuntimeError("独立检查列表与当前任务授权不匹配。")
        if len(results) < len(checks):
            self._run_next_check(state, work, task["candidate"]["head_sha"], results, "check", checks)
        if len(results) == len(checks):
            state["phase"] = "REVIEW"

    def _run_next_check(self, state, work, expected_head, results, kind, checks):
        if not self._skip_checks_after_failed_editor_build(checks, results, expected_head):
            result = self._independent_check(state, work, expected_head, checks[len(results)], kind)
            if result is None:
                return
            results.append(result)
            self._skip_checks_after_failed_editor_build(checks, results, expected_head)
        state["operation"] = None

    @staticmethod
    def _skip_checks_after_failed_editor_build(checks, results, expected_head):
        # A failed editor build leaves binaries that may belong to an older HEAD.
        failed = any(result["id"] == "ue-editor-build" and result["head_sha"] == expected_head and
                     not result["passed"] for result in results)
        if not failed:
            return False
        for check in checks[len(results):]:
            results.append({"id": check["id"], "head_sha": expected_head, "passed": False,
                            "status": "NOT_RUN", "reason": "editor_build_failed", "prerequisite": "ue-editor-build",
                            "exit_code": None, "timed_out": False, "output_limit_exceeded": False,
                            "patterns": {"missing_success": [], "matched_failure": []}, "argv": [],
                            "stdout_path": None, "stderr_path": None})
        return True

    def _independent_check(self, state, work, expected_head, check, kind):
        """One restartable configured check bound to a fixed clean commit."""
        if gitops.head(work) != expected_head or not gitops.clean(work):
            raise RuntimeError("独立检查前候选提交或工作区改变。")
        op = state["operation"] or self._new_operation(state, kind)
        if op["kind"] != kind:
            raise RuntimeError("检查断点不匹配。")
        if op.get("check_id") not in (None, check["id"]):
            raise RuntimeError("检查断点绑定了其他独立检查项。")
        op["check_id"] = check["id"]
        directory = Path(op["output_dir"])
        receipt = directory / "check.json"
        if receipt.exists():
            result = read_json(receipt)
        else:
            # Interrupted tests may have left output. Preserve it and run the
            # same configured command afresh; no model call or commit is repeated.
            if directory.exists():
                state["operation"] = None
                op = self._new_operation(state, kind)
                op["check_id"] = check["id"]
                directory = Path(op["output_dir"])
                receipt = directory / "check.json"
            timeout, window_limited = self._timeout(check["timeout_seconds"])
            op["window_limited"] = window_limited
            self._save(state)
            argv = [x.replace("{workspace}", str(work)).replace("{runtime}", str(self.runtime)) for x in check["argv"]]
            process = run_process(argv, cwd=work, output_dir=directory, timeout_seconds=timeout,
                                  environment=safe_environment(), max_output_bytes=16 * 1024 * 1024)
            if process.timed_out and window_limited:
                state["operation"] = None
                self._save(state)
                return
            output = process.stdout_path.read_text(encoding="utf-8", errors="replace") + "\n" + process.stderr_path.read_text(encoding="utf-8", errors="replace")
            patterns = {"missing_success": [x for x in check.get("success_patterns", []) if x not in output],
                        "matched_failure": [x for x in check.get("failure_patterns", []) if x in output]}
            result = {"id": check["id"], "head_sha": expected_head,
                      "exit_code": process.exit_code, "timed_out": process.timed_out,
                      "output_limit_exceeded": process.output_limit_exceeded,
                      "patterns": patterns, "argv": argv,
                      "passed": process.exit_code == 0 and not process.timed_out and not process.output_limit_exceeded and not any(patterns.values()),
                      "stdout_path": str(process.stdout_path), "stderr_path": str(process.stderr_path)}
            write_json(receipt, result)
        if (result["id"] != check["id"] or result["head_sha"] != expected_head or
                gitops.head(work) != expected_head or not gitops.clean(work)):
            raise RuntimeError("验收命令修改了提交或留下未提交内容，检查证据已失效。")
        return result

    def _review(self, state, work):
        self._user_followup_gate(state["task"])
        task = state["task"]
        current = gitops.head(work)
        if current != task["candidate"]["head_sha"] or not gitops.clean(work):
            raise RuntimeError("验收前候选提交被改变。")
        prompt = self._base_prompt() + (
            f"你是 Codex 独立验收者，固定受审 HEAD={current}。只审阅，不能编码、提交或推送。\n"
            "读取实际 diff、代码、交付报告和下列机器检查日志，逐条验证任务书的验收标准。\n"
            "代码可读不代表运行/画面已通过。已有授权、工具和项目范围内可以修复、重跑或补采的缺项，必须返回 rework 并给出同任务可执行要求。\n"
            "只有必须等待外部权限、用户必要决策或当前不可获得资源等无法自主处理的条件时，才返回 blocked，并明确缺少的外部条件。\n"
            "返回 JSON：decision=accepted/rework/blocked，head_sha必须为上述完整HEAD。\n任务与证据：\n" +
            json.dumps(task, ensure_ascii=False))
        if task.get("user_repair"):
            prompt += (
                "\n本任务有用户明确追加的完整修复授权：以 user_repair 绑定的请求原文、证据和任务书为范围，"
                "逐项核对交付报告（含后续工具交付）中已知残留和未验证项，不能只审本任务新增的回归。"
                "不能仅因旧文档曾冻结、问题并非本任务引入，便自动搁置已在本次授权范围内的问题。"
                "合理的技术诊断文字和确实范围外的历史人工待办可以保留，但须逐项说明保留依据；"
                "不据此扩展到整个 M3、M4 或其他未授权工作。"
                "范围内可自主修复、验证或补证的缺项应返回 rework，说明应达到的结果和缺少的证据，"
                "由 Claude 自行提出并实施方案，不替它规定具体实现配方。"
                "仍依据实际证据独立判断，不预设本轮通过或失败，不改变任何机器检查门槛。\n")
        result = self._invoke(state, "review", prompt, work)
        if result["head_sha"] != current or gitops.head(work) != current or not gitops.clean(work):
            raise RuntimeError("验收结果没有绑定当前候选 HEAD。")
        write_json(self.runtime / "tasks" / task["id"] / f"review-{task['reworks']}.json", result)
        state["operation"] = None
        checks_passed = all(x["passed"] for x in task["check_results"])
        repair_passed = self._user_repair_gate(task, work, require_pass=False)
        checks_passed = checks_passed and repair_passed
        if result["decision"] == "accepted" and not checks_passed:
            gate = self._verified_task_validation_gate(state, work) if repair_passed else None
            if gate is not None:
                self._record_task_validation_gate(state, gate)
                checks_passed = gate["gate_passed"]
        if result["decision"] == "blocked":
            self._block(state, "REVIEW", result["feedback"] or result["summary"])
        elif result["decision"] == "rework" or not checks_passed:
            if task["reworks"] >= (self.config["max_reworks"] + task.get("manual_rework_allowance", 0)
                                    + task.get("autonomous_rework_allowance", 0)):
                if self._begin_task_replanning(state, result["feedback"] or result["summary"], trigger="review_limit"):
                    return
                self._block(state, "REVIEW", "已达到返工次数上限。" + result["feedback"])
                return
            task["reworks"] += 1
            task["feedback"] = result["feedback"] + "\n独立检查：" + json.dumps(task["check_results"], ensure_ascii=False)
            task["code_base"], task["code_resume"] = current, False
            state["phase"] = "CODE"
        else:
            state["phase"] = "PUBLISH"

    def _publish(self, state, work):
        self._user_followup_gate(state["task"])
        task = state["task"]
        expected = task["candidate"]["head_sha"]
        if gitops.head(work) != expected or not gitops.clean(work):
            raise RuntimeError("交付前受审提交被改变。")
        self._user_repair_gate(task, work, require_pass=True)
        if task.get("validation_gate"):
            gate = self._verified_task_validation_gate(state, work)
            previous = task["validation_gate"]
            if gate is None or any(previous.get(key) != value for key, value in gate.items()):
                raise ValueError("交付前可信门槛策略或绑定证据已变化。")
        milestone = self._publication()
        # Milestone mode always wins over the legacy push switch. Individual
        # tasks remain local even when an old configuration still says push=true.
        publication = ({"local_only": True, "deferred_to_milestone": milestone["milestone_id"]} if milestone else
                       gitops.publish(work, self.config, expected) if self.config["push"] else {"local_only": True})
        record = {"id": task["id"], "title": task["title"], "head_sha": expected,
                  "base_sha": task["base_sha"], "accepted_at": self.now().isoformat(), "publication": publication}
        write_json(self.runtime / "tasks" / task["id"] / "completed-task.json", task)
        write_json(self.runtime / "tasks" / task["id"] / "accepted.json", record)
        state["accepted"].append(record)
        state["expected_head"], state["task"] = expected, None
        state["sequence"] += 1
        state["phase"] = "PLAN"

    def _milestone_candidate(self, state, work):
        publication, milestone = self._publication(), state.get("milestone")
        if (not publication or not milestone or milestone["id"] != publication["milestone_id"] or
                milestone["goal"] != publication["goal"] or milestone["proposal"].get("action") != "complete"):
            raise RuntimeError("阶段候选与当前发布目标不匹配。")
        if milestone.get("milestone_checks", []) != self.config.get("milestone_checks", []):
            raise RuntimeError("里程碑候选的阶段检查配置发生变化，无法继续使用该候选。")
        pending = self._pending_milestone_tasks(state)
        deferred = self._deferred_tasks(state)
        if ((not pending and not deferred) or [record["id"] for record in pending] != milestone["task_ids"] or
                deferred != milestone.get("deferred_tasks", [])):
            raise RuntimeError("阶段候选的本地任务集合发生变化。")
        expected = milestone["head_sha"]
        if (expected not in [record["head_sha"] for record in pending + deferred] or state["expected_head"] != expected or
                gitops.head(work) != expected or not gitops.clean(work)):
            raise RuntimeError("阶段候选 HEAD 或工作区发生变化，原验收证据已失效。")
        return milestone

    def _milestone_evidence(self, milestone):
        records = []
        for accepted in milestone["accepted_tasks"]:
            directory = self.runtime / "tasks" / accepted["id"]
            records.append({"accepted": accepted, "directory": str(directory),
                            "task_path": str(directory / "TASK.md"),
                            "completed_task_path": str(directory / "completed-task.json"),
                            "review_paths": [str(path) for path in sorted(directory.glob("review-*.json"))]})
        for deferred in milestone.get("deferred_tasks", []):
            directory = self.runtime / "tasks" / deferred["id"]
            records.append({"deferred": deferred, "directory": str(directory),
                            "task_path": str(directory / "TASK.md"), "deferred_task_path": deferred["receipt_path"],
                            "review_paths": [str(path) for path in sorted(directory.glob("review-*.json"))]})
        return records

    def _milestone_rework(self, state, feedback):
        milestone = state["milestone"]
        milestone["status"] = "rework"
        state["milestone_feedback"] = feedback
        state["operation"], state["phase"] = None, "PLAN"

    def _milestone_review(self, state, work):
        milestone = self._milestone_candidate(state, work)
        results = milestone["check_results"]
        checks = self.config["checks"] + milestone.get("milestone_checks", [])
        # These are fresh integration checks at the final stage HEAD, separate
        # from the earlier per-task checks. Run at most one command per step.
        if len(results) < len(checks):
            self._run_next_check(state, work, milestone["head_sha"], results, "milestone_check", checks)
            return
        if not all(result["passed"] for result in results):
            self._milestone_rework(state, "阶段独立检查未通过，需规划有限修正任务；不得宣布整个阶段完成。\n" +
                                   json.dumps(results, ensure_ascii=False))
            return
        for accepted in milestone["accepted_tasks"] + milestone.get("deferred_tasks", []):
            gitops.git(work, "merge-base", "--is-ancestor", accepted["head_sha"], milestone["head_sha"])
        prompt = self._base_prompt() + (
            f"你是独立的 Codex 阶段审核者。核验整个里程碑 {milestone['id']} 的系统完成条件，固定 HEAD={milestone['head_sha']}。\n"
            "项目经理的 complete 仅为提案。逐项核对阶段 goal、Roadmap、仍开放的审查问题、全部本地任务及实际代码。\n"
            "读取下列任务书、完成快照、原始日志和审查记录，并核对 final HEAD 的全新独立检查证据。\n"
            "系统完成条件必须有相应真实证据，包括所需的画面或实际运行证据。逐项说明仍未验证的系统行为。\n"
            "用户已登记的延期人工确认事项保持 pending_manual_confirmation，在审核结论中写明任务编号及待办内容。\n"
            "系统部分有完整证据且通过时返回 accepted。已登记的人工待确认项单独保留，可以与系统发布并存。\n"
            "系统功能缺失或证据缺口可修复时返回 rework。缺少系统验证所需且无法获得的外部条件时返回 blocked。\n"
            "只审阅并返回结论，head_sha 必须为上述完整 HEAD。\n"
            "阶段提案：\n" + json.dumps(milestone["proposal"], ensure_ascii=False) + "\n"
            "全部本地任务与证据入口：\n" + json.dumps(self._milestone_evidence(milestone), ensure_ascii=False) + "\n"
            "阶段最终独立检查：\n" + json.dumps(results, ensure_ascii=False))
        result = self._invoke(state, "review", prompt, work)
        self._milestone_candidate(state, work)
        if result["head_sha"] != milestone["head_sha"]:
            raise RuntimeError("阶段验收结果没有绑定候选 HEAD。")
        directory = self.runtime / "milestones" / milestone["id"]
        review_path = directory / f"review-{milestone['attempt']}.json"
        write_json(review_path, result)
        milestone["review"], milestone["review_path"] = result, str(review_path)
        state["operation"] = None
        if result["decision"] == "accepted":
            milestone["status"], state["phase"] = "accepted", "MILESTONE_PUBLISH"
        elif result["decision"] == "rework":
            self._milestone_rework(state, result["feedback"] or result["summary"])
        else:
            self._block(state, "MILESTONE_REVIEW", result["feedback"] or result["summary"])

    def _milestone_publish(self, state, work):
        milestone = self._milestone_candidate(state, work)
        review, results = milestone.get("review"), milestone["check_results"]
        checks = self.config["checks"] + milestone.get("milestone_checks", [])
        expected = milestone["head_sha"]
        if (not review or review.get("decision") != "accepted" or review.get("head_sha") != expected or
                len(results) != len(checks) or not results or
                any(result.get("id") != check["id"] or result.get("head_sha") != expected or
                    result.get("passed") is not True for result, check in zip(results, checks))):
            raise RuntimeError("阶段发布缺少绑定当前 HEAD 的独立验收与完整检查。")
        publication = gitops.publish(work, self.config, expected)
        deferred = milestone.get("deferred_tasks", [])
        historical_deferred = [record for record in state.get("deferred", [])
                               if record.get("milestone_id") != milestone["id"] and
                               record.get("status") == "pending_manual_confirmation"]
        historical_milestones = [
            {"id": entry["milestone"]["id"], "status": entry["status"],
             "review_path": entry["milestone"].get("review_path"),
             "advance_audit_path": entry.get("transition", {}).get("audit_path")}
            for entry in state.get("milestone_history", [])
            if entry.get("status") == "pending_manual_confirmation"
        ]
        manual_pending = [record["id"] for record in deferred if record.get("status") == "pending_manual_confirmation"]
        system_verification = {"status": "passed", "head_sha": expected}
        manual_acceptance = {"status": "pending_manual_confirmation" if manual_pending else "not_recorded",
                             "pending_task_ids": manual_pending}
        manual_summary = f"人工验收仍有 {len(manual_pending)} 项待确认。" if manual_pending else "人工验收暂无独立确认记录。"
        report = {"milestone_id": milestone["id"], "goal": milestone["goal"],
                  "base_sha": milestone["base_sha"], "head_sha": expected,
                  "task_ids": milestone["task_ids"], "accepted_tasks": milestone["accepted_tasks"],
                  "deferred_tasks": deferred, "system_verification": system_verification, "manual_acceptance": manual_acceptance,
                  "historical_deferred_tasks": historical_deferred, "historical_pending_milestones": historical_milestones,
                  "checks": results, "review": review, "review_path": milestone["review_path"],
                  "publication": publication, "published_at": self.now().isoformat()}
        directory = self.runtime / "milestones" / milestone["id"]
        write_json(directory / "report.json", report)
        report_text = ("# " + milestone["id"] + " 阶段交付\n\n" +
                       "目标：" + milestone["goal"] + "\n\nFinal HEAD: " + expected +
                       "\n\n独立检查与 Codex 系统审核通过，远端工作分支已核对。\n\n" + manual_summary +
                       "\n\n## 已验收任务\n\n" + "\n".join("- " + task_id for task_id in milestone["task_ids"]) +
                       "\n\n## 人工待办\n\n" + "\n".join("- " + record["id"] + " · " + record["status"] + " · " + record["reason"]
                                                              for record in deferred) + "\n")
        if historical_deferred:
            report_text += "\n## 历史阶段人工待办\n\n" + "\n".join(
                f"- {record.get('milestone_id') or '未登记阶段'} · {record['id']} · {record['status']} · {record['reason']}"
                for record in historical_deferred) + "\n"
        if historical_milestones:
            report_text += "\n## 归档阶段待人工确认记录\n\n"
            for record in historical_milestones:
                report_text += f"- {record['id']} · {record['status']}\n"
                if record["review_path"]:
                    report_text += f"  - 审核回执 `{record['review_path']}`\n"
                if record["advance_audit_path"]:
                    report_text += f"  - 阶段切换审计 `{record['advance_audit_path']}`\n"
        write_text(directory / "REPORT.md", report_text)
        for accepted in state["accepted"]:
            if accepted["id"] in milestone["task_ids"]:
                accepted["milestone_publication"] = {"id": milestone["id"], "head_sha": expected}
        milestone["publication"], milestone["status"] = publication, "published"
        milestone["system_verification"], milestone["manual_acceptance"] = system_verification, manual_acceptance
        milestone["report_path"] = str(directory / "report.json")
        state["phase"], state["operation"] = "DONE", None
        state["completion_reason"] = milestone["id"] + " 已通过系统验证并集中推送。" + manual_summary

    @staticmethod
    def _block(state, phase, reason):
        state["blocked_from"], state["phase"], state["last_error"] = phase, "BLOCKED", reason

    def _workspace(self):
        return gitops.ensure_workspace(self.config)

    def _checks_ready(self):
        return bool(self.config["checks"])

    def _step(self):
        state = self._load()
        if (state.get("task") or {}).get("kind") == "art":
            self.consume_art_diagnosis_response(state)
        if state["phase"] == "DONE" and any(item.get("status") == "queued" or
                (item.get("status") == "awaiting_approval" and item.get("decision")) for item in self.art_queue()["requests"]):
            state["art_return_done"] = {"completion_reason": state.get("completion_reason")}
            state["phase"] = "PLAN"
            state.pop("completion_reason", None)
        if (self.runtime / "PAUSE").exists():
            return self._snapshot(state)
        if (state.get("task") or {}).get("user_followup", {}).get("status") not in {"active", "exhausted"} and self._recover_code_timeout(state, self.runtime / "workspace"):
            self._save(state)
            write_json(self.runtime / "heartbeat.json", self._snapshot(state))
            return self._snapshot(state)
        if state["phase"] in {"DONE", "BLOCKED"}:
            return self._snapshot(state)
        if not self._checks_ready():
            return dict(self._snapshot(state), status="needs_setup", ready=False,
                        last_error="请先配置 checks 为项目实际可执行的独立验收命令。")
        calendar = self._calendar()
        description = calendar.describe(self.now())
        if not description["calendar_covered"]:
            return dict(self._snapshot(state), status="calendar_update_required")
        if not calendar.allowed(self.now()):
            return dict(self._snapshot(state), status="waiting_window")
        phase = state["phase"]
        try:
            if state.get("provider_retry") is not None:
                deadline = self._validate_provider_retry(state, self.runtime / "workspace")
                if state["provider_retry"]["status"] == "waiting" and self.now() < deadline:
                    return dict(self._snapshot(state), status="waiting_provider")
            closing = calendar.seconds_until_close(self.now()) if self.config["close_policy"] == "strict" else None
            with gitops.deadline(closing):
                work = self._workspace()
                if state["expected_head"] is None:
                    state["expected_head"] = gitops.head(work)
                    self._save(state)
                if phase == "PLAN" and gitops.head(work) != state["expected_head"]:
                    raise RuntimeError("工作分支的已验收 HEAD 被外部改变。")
                getattr(self, "_" + phase.lower())(state, work)
            state["failures"] = 0
            state.pop("provider_retry", None)
            if state["phase"] != "BLOCKED":
                state["last_error"] = None
        except gitops.WindowClosed:
            self._save(state)
            return dict(self._snapshot(state), status="waiting_window")
        except Exception as error:
            # A temporarily locked checkpoint is infrastructure recovery, not
            # a model/code failure. The background supervisor reloads the last
            # durable state; do not spend the task's failure budget here.
            if is_windows_sharing_error(error):
                raise
            op = state.get("operation")
            if phase == "CODE" and state["task"]:
                session = getattr(error, "session_id", None)
                if session:
                    try:
                        self._claim_session(state["task"], session)
                    except RuntimeError as ownership_error:
                        error = ownership_error
                state["task"]["code_resume"] = True
            if op and op.get("window_limited") and getattr(error, "interrupted", False):
                state["operation"] = None
                if state.get("provider_retry"):
                    state["provider_retry"].update(status="retrying", operation_output_dir=None)
                self._save(state)
                return dict(self._snapshot(state), status="waiting_window")
            if phase == "CODE" and (state.get("task") or {}).get("user_followup", {}).get("status") not in {"active", "exhausted"} and self._recover_code_timeout(state, self.runtime / "workspace", error=error):
                self._save(state)
                write_json(self.runtime / "heartbeat.json", self._snapshot(state))
                return self._snapshot(state)
            if phase == "CODE" and self._followup_failure_limit(state, error):
                write_json(self.runtime / "heartbeat.json", self._snapshot(state))
                return self._snapshot(state)
            if isinstance(error, ProviderError) and getattr(error, "transient", False) is True:
                try:
                    return self._schedule_provider_retry(state, phase, error, self.runtime / "workspace")
                except Exception as retry_error:
                    error = retry_error
            state.pop("provider_retry", None)
            self._retire_rejected_provider_output(state, error)
            state["failures"] += 1
            state["last_error"] = f"{type(error).__name__}: {error}"
            if state["failures"] >= self.config["max_failures"]:
                self._block(state, phase, state["last_error"])
        self._save(state)
        write_json(self.runtime / "heartbeat.json", self._snapshot(state))
        return self._snapshot(state)

    def step(self):
        with self._locks():
            from .burst import resume_pending_burst
            pending = resume_pending_burst(self, once=True)
            if pending is not None:
                return pending
            return self._step()

    def run(self, once=False):
        if once:
            return self.step()
        with self._locks():
            from .burst import resume_pending_burst
            pending_summary = None
            while True:
                pending = resume_pending_burst(self)
                if pending is None:
                    break
                summary = (pending["status"], pending.get("phase"), pending.get("task_id"),
                           pending.get("completed_tasks"), pending.get("last_error"))
                if summary != pending_summary:
                    print(json.dumps({"event": "burst_recovered", **pending}, ensure_ascii=False), flush=True)
                    pending_summary = summary
                write_json(self.runtime / "heartbeat.json", dict(pending, heartbeat_at=self.now().isoformat()))
                if pending["status"] == "completed":
                    break
                if pending["status"] in {"waiting_window", "calendar_update_required", "paused", "waiting_provider"}:
                    # A login worker has no separate evening trigger. Stay
                    # alive under the same locks until this batch can resume;
                    # never clear PAUSE or fall through to the normal loop.
                    time.sleep(self._wait_seconds(pending))
                    continue
                return pending
            # Keep the same locks across this boundary. The normal loop now
            # uses this Engine's untouched, original work calendar.
            previous = None
            while True:
                result = self._step()
                # Heartbeats continue while idle/paused without model requests.
                write_json(self.runtime / "heartbeat.json", dict(result, heartbeat_at=self.now().isoformat()))
                summary = (result["status"], result.get("task_id"), result.get("last_error"))
                if summary != previous:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    previous = summary
                if result["status"] in {"done", "blocked", "needs_setup"}:
                    return result
                if result["status"] in {"waiting_window", "paused", "calendar_update_required", "waiting_provider"} or result["failures"]:
                    time.sleep(self._wait_seconds(result))
