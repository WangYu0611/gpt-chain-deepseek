"""Bounded, read-only critical planning for the same rejected development task.

A repair plan adds execution guidance. It never replaces the task, its tests,
acceptance criteria, coding session, accepted baseline, or independent reviewer.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from . import gitops
from .providers import ProviderError, read_session, validate_output
from .storage import read_json, write_json
from .ue_checks import _safe_under


class TaskReplanning:
    @staticmethod
    def _replanning_digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _task_replanning_policy(self, task=None):
        if task and task.get("user_repair"):
            profile = self._active_repair_profile(task)
            return {"enabled": True, "max_cycles": profile["max_cycles"], "rework_window": profile["rework_window"]}
        from .config import validate_task_replanning_settings
        value = self.config.get("task_replanning")
        return validate_task_replanning_settings(value) if value is not None else {
            "enabled": False, "max_cycles": 1, "rework_window": 1}

    @staticmethod
    def _epoch_replanning_history(task):
        history = task.get("replanning_history", [])
        repair = task.get("user_repair")
        return [row for row in history if row.get("user_repair_epoch") == repair["epoch"]] if repair else history

    def _code_timeout_receipt(self, task, work, op):
        """A wall-budget receipt is authority; error text alone never is."""
        if (not isinstance(op, dict) or op.get("kind") != "code" or op.get("task_id") != task.get("id") or
                op.get("coding_provider") != "claude" or task.get("coding_provider") != "claude" or
                self.config.get("coding_provider") != "claude" or task.get("kind") == "art" or
                op.get("window_limited") is not False or
                op.get("code_base", op.get("head_sha")) != task.get("code_base") or
                not re.fullmatch(r"[0-9a-f]{40}", op.get("head_sha", "")) or
                task.get("session_owner_task_id") != task.get("id")):
            raise ValueError("CODE 超时操作与当前任务、提供方或原 HEAD 不匹配。")
        out = Path(op.get("output_dir", "")).absolute()
        runs = (self.runtime / "runs").absolute()
        if out.parent != runs or not re.fullmatch(r"code-[0-9a-f]{32}", out.name):
            raise ValueError("CODE 超时回执目录不属于当前操作。")
        out = _safe_under(runs, out.name)
        path = _safe_under(out, "process.json")
        if not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("CODE 超时缺少受信进程回执。")
        record = read_json(path)
        session = task.get("session_id")
        requested = op.get("requested_session_id", session)
        observed = read_session(out, requested_session=requested)
        repair = task.get("user_repair")
        if repair:
            from .user_repair import read_repair_session
            if (record.get("user_repair_profile") != self._active_repair_profile(task) or
                    record.get("native_protocol") != "full_flow_native_v1" or record.get("task_id") != task["id"] or
                    record.get("head_sha") != op["head_sha"] or record.get("coding_model") != repair["model"] or
                    record.get("coding_reasoning_effort") != repair["reasoning_effort"] or
                    record.get("strict_model_selection") is not True or op.get("user_repair_id") != repair["id"] or
                    op.get("user_repair_request_sha256") != repair["request_sha256"]):
                raise ValueError("Repair CODE timeout does not match its explicit authorization")
            observed = read_repair_session(task, self.runtime, out)
        if (not isinstance(session, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", session) or
                record.get("kind") != "code" or record.get("coding_provider") != "claude" or
                record.get("timed_out") is not True or record.get("output_limit_exceeded") is not False or
                record.get("launch_or_capture_failed") or type(record.get("exit_code")) is not int or
                (record.get("session_id") not in (None, session) if repair else record.get("session_id") != session) or
                record.get("requested_session") != requested or requested not in (None, session) or observed != session):
            raise ValueError("CODE 预算中断或原生会话证据不匹配。")
        return out

    def _code_wip_snapshot(self, work):
        """Hash only Git-reported changes; never traverse ignored/runtime trees."""
        tracked = gitops.git(work, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--")
        untracked = gitops.git(work, "ls-files", "--others", "--exclude-standard", "-z")
        names = sorted(set(filter(None, (tracked + "\0" + untracked).split("\0"))))
        if len(names) > 512:
            raise ValueError("CODE WIP 超过安全快照文件数量。")
        rows, total = [], 0
        for name in names:
            if not name or any(part == ".git" for part in Path(name).parts):
                raise ValueError("CODE WIP 包含无效路径。")
            path = _safe_under(work, name)
            if not path.exists():
                rows.append({"path": name, "sha256": None, "size": None})
                continue
            before = path.stat()
            total += before.st_size
            if not path.is_file() or before.st_size > 32 * 1024**2 or total > 128 * 1024**2:
                raise ValueError("CODE WIP 文件超出安全快照范围。")
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("CODE WIP 在快照期间改变。")
            rows.append({"path": name, "sha256": digest, "size": before.st_size})
        return {"files": rows, "index_sha256": self._replanning_digest(gitops.git(
            work, "diff", "--no-ext-diff", "--raw", "-z", "--cached", "HEAD", "--")),
                "worktree_sha256": self._replanning_digest(gitops.git(
            work, "diff", "--no-ext-diff", "--raw", "-z", "HEAD", "--"))}

    def _replanning_source(self, task, work, *, source_phase=None, source_operation=None):
        if (not isinstance(task, dict) or task.get("kind") == "art" or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", task.get("id", ""))):
            raise ValueError("重新规划只允许当前普通功能任务。")
        effective_checks = self._task_checks(task)
        pending = task.get("replanning") or {}
        source_phase = source_phase or pending.get("source_phase", "REVIEW")
        code_timeout = source_phase == "CODE"
        timeout_op = source_operation if source_operation is not None else pending.get("source_operation")
        out = self._code_timeout_receipt(task, work, timeout_op) if code_timeout else None
        head = gitops.head(work)
        if not code_timeout and (head != task.get("candidate", {}).get("head_sha") or not gitops.clean(work)):
            raise ValueError("重新规划需要干净且匹配当前候选的 HEAD。")
        round_ = task.get("reworks")
        if type(round_) is not int or round_ < 0:
            raise ValueError("重新规划缺少有效返工轮次。")
        folder = _safe_under(self.runtime, Path("tasks") / task["id"])
        files = {}
        initial_code = False
        if code_timeout:
            # A coding attempt is newer than its last completed independent review.
            round_ = next((i for i in range(round_ - 1, -1, -1)
                           if (folder / f"review-{i}.json").is_file()), None)
            if round_ is None:
                initial_code = (task["reworks"] == 0 and not task.get("candidate") and
                                not task.get("check_results"))
                if not initial_code:
                    raise ValueError("CODE 超时重新规划缺少上一轮已完成审核。")

        def evidence(path, root):
            path = Path(path).absolute()
            root = Path(root).absolute()
            if not path.is_relative_to(root):
                raise ValueError("重新规划证据越出当前任务的受信目录。")
            path = _safe_under(root, path.relative_to(root))
            if not path.is_file():
                raise ValueError("重新规划证据文件缺失。")
            with path.open("rb") as source:
                files[str(path)] = hashlib.file_digest(source, "sha256").hexdigest()
            return path

        review_path, delivery_path, review_head, checks = None, None, None, []
        if not initial_code:
            review_path = evidence(folder / f"review-{round_}.json", folder)
            review = validate_output("review", read_json(review_path))
            review_head = review["head_sha"] if code_timeout else head
            checks = task.get("check_results") or (pending.get("source_check_results") if code_timeout else None)
            if (not isinstance(checks, list) or not checks or
                    [row.get("id") for row in checks] != [row["id"] for row in effective_checks] or
                    any(row.get("head_sha") != review_head or type(row.get("passed")) is not bool for row in checks)):
                raise ValueError("重新规划需要审核 HEAD 的完整独立检查记录。")
            if (review["head_sha"] != review_head or review_head != task.get("candidate", {}).get("head_sha") or
                    review["decision"] == "blocked" or
                    review["decision"] == "accepted" and all(row["passed"] for row in checks)):
                raise ValueError("重新规划必须绑定当前候选的实际返工意见，不能越过外部阻塞或已通过验收。")
            delivery_path = evidence(folder / f"delivery-{round_}.md", folder)
        for row in checks:
            for key in ("stdout_path", "stderr_path"):
                if row.get(key):
                    path = evidence(row[key], self.runtime / "runs")
                    receipt = path.parent / "check.json"
                    if receipt.is_file():
                        evidence(receipt, self.runtime / "runs")
        for name in ("task.json", "TASK.md"):
            if (folder / name).exists():
                evidence(folder / name, folder)
        # Bind the current independently produced UE receipt and its exact logs,
        # not unrelated historical attempts or a recursively discovered tree.
        mirror = _safe_under(work, Path("Saved/Automation") / task["id"] / "validation-results.json")
        if mirror.exists():
            value = read_json(evidence(mirror, work))
            if value.get("task_id") != task["id"] or value.get("head_sha") != review_head:
                raise ValueError("外层验证摘要不属于当前任务或 HEAD。")
            receipt_path = evidence(value["receipt_path"], folder / "validation")
            for row in value.get("tests", []):
                for key in ("stdout_path", "stderr_path"):
                    if row.get(key):
                        evidence(row[key], receipt_path.parent)
        scope = {key: deepcopy(task.get(key)) for key in
                 ("id", "title", "body", "acceptance_criteria", "core_tests", "base_sha", "coding_provider")}
        scope["checks"] = deepcopy(effective_checks)
        result = {"head_sha": head, "review_round": round_, "scope_sha256": self._replanning_digest(scope),
                "review_sha256": files.get(str(review_path)), "delivery_sha256": files.get(str(delivery_path)),
                "check_results_sha256": self._replanning_digest(checks), "evidence_files": files,
                "coding_session_sha256": self._replanning_digest(task.get("session_id")),
                "coding_settings_sha256": self._replanning_digest({"provider": self.config.get("coding_provider"),
                    "model": self.config.get("claude", {}).get("model"),
                    "reasoning_effort": self.config.get("claude", {}).get("reasoning_effort")})}
        if code_timeout:
            for name in ("process.json", "stdout.log", "stderr.log"):
                evidence(out / name, out)
            # Fixed per-attempt core log names; no arbitrary runtime glob.
            log_root = _safe_under(work, Path("Saved/Logs") / out.name)
            for index in range(1, len(task.get("core_tests") or []) + 1):
                log = _safe_under(log_root, f"core-{index}.log")
                if log.is_file():
                    evidence(log, work)
            tool_settings = self.config.get("claude", {})
            for option, field in (("test_preflight", "commands"), ("visual_preflight", "argv")):
                scoped = tool_settings.get(option) or {}
                if scoped.get("task_id") != task["id"]:
                    continue
                commands = scoped.get(field, []) if field == "commands" else [scoped.get(field, [])]
                for command in commands:
                    for arg in command:
                        if not isinstance(arg, str) or not arg.lower().startswith("-abslog="):
                            continue
                        value = arg.partition("=")[2].replace("{workspace}", str(work)).replace(
                            "{output_dir}", str(log_root)).replace("{runtime}", str(self.runtime))
                        log = Path(value).absolute()
                        if log.is_relative_to(log_root) and log.is_file():
                            evidence(log, log_root)
            wip = self._code_wip_snapshot(work)
            result.update(source_phase="CODE", review_head_sha=review_head, wip=wip,
                          review_status="not_yet_created" if initial_code else "previous_completed_review",
                          wip_sha256=self._replanning_digest(wip), timeout_operation=deepcopy(timeout_op),
                          code_base=task.get("code_base"), tool_settings_sha256=self._replanning_digest({
                              key: tool_settings.get(key) for key in
                              ("ue_preflight", "planned_core_tests", "test_preflight", "visual_preflight")}))
        return result

    def _recover_code_timeout(self, state, work, *, error=None):
        """One durable decision per native timeout, including refused recovery."""
        if (self.runtime / "PAUSE").exists():
            return False
        task, op = state.get("task") or {}, state.get("operation")
        if (not self._task_replanning_policy(task)["enabled"] or task.get("kind") == "art" or
                state.get("phase") not in {"CODE", "BLOCKED"} or
                state.get("phase") == "BLOCKED" and state.get("blocked_from") != "CODE" or
                not isinstance(op, dict) or op.get("kind") != "code"):
            return False
        if error is not None and not (isinstance(error, ProviderError) and error.interrupted):
            return False
        if error is None and state.get("last_error") and state["last_error"] != "ProviderError: Provider exceeded its time limit":
            return False
        # Missing/non-timeout receipts are ordinary in-flight operations, not refusals.
        try:
            out = self._code_timeout_receipt(task, work, op)
        except (ValueError, OSError, TypeError, KeyError):
            return False
        key = self._replanning_digest(op)
        decisions = task.setdefault("code_timeout_recoveries", [])
        if any(row.get("operation_sha256") == key for row in decisions):
            return False
        decision = {"operation_sha256": key, "output_dir": str(out), "at": self.now().isoformat(),
                    "previous_error": f"{type(error).__name__}: {error}" if error is not None else state.get("last_error"),
                    "previous_failures": state.get("failures", 0)}
        try:
            if gitops.head(work) != task.get("code_base") and gitops.clean(work):
                # The original _code path owns candidate validation and delivery recovery.
                task["code_resume"] = True
                state.update(phase="CODE", last_error=None)
                state.pop("blocked_from", None)
                decision["decision"] = "recover_committed_code"
            else:
                self._begin_task_replanning(state, "本次 CODE 原生执行达到预算；保留未提交工作，请依据真实日志诊断后继续原任务。",
                                            trigger="code_timeout", source_phase="CODE", source_operation=op)
                decision["decision"] = "replan" if state["phase"] == "REPLAN" else "refused"
                if state["phase"] == "REPLAN":
                    # A new read-only planning stage gets its own failure budget;
                    # interrupted CODE failures remain in the durable audit above.
                    state["failures"] = 0
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as failure:
            self._block(state, "CODE", "CODE 超时恢复校验拒绝：" + str(failure))
            decision["decision"] = "refused"
        decision["reason"] = state.get("last_error")
        decisions.append(decision)
        return True

    def _begin_task_replanning(self, state, feedback, *, trigger, source_phase="REVIEW", source_operation=None):
        task = state.get("task") or {}
        policy = self._task_replanning_policy(task)
        if not policy["enabled"]:
            return False
        task = state.get("task") or {}
        settings = self.config.get("claude", {})
        if (task.get("kind") == "art" or task.get("coding_provider") != "claude" or
                self.config.get("planning_provider") != "claude" or
                self.config.get("coding_provider") != "claude" or not settings.get("critical_plan_model") or
                not settings.get("critical_plan_reasoning_effort")):
            raise ValueError("同任务重新规划需要已配置的 Claude 关键规划模型和原 Claude 编码任务。")
        history = self._epoch_replanning_history(task)
        if len(history) >= policy["max_cycles"]:
            self._block(state, source_phase, "同任务重新规划窗口已用完；原验收仍未通过。" + feedback)
            return True
        source = self._replanning_source(task, self.runtime / "workspace", source_phase=source_phase,
                                         source_operation=source_operation)
        if any(row.get("head_sha") == source["head_sha"] and row.get("execution_decision") == "continue"
               and (source_phase != "CODE" or row.get("source_fingerprint", {}).get("wip_sha256") == source["wip_sha256"])
               for row in history):
            self._block(state, source_phase, "同一候选 HEAD 和工作内容已执行过重新规划，不能重复放宽执行窗口。")
            return True
        task["replanning"] = {"round": len(task.get("replanning_history", [])) + 1, "status": "pending", "trigger": trigger,
                              "source_phase": source_phase, "feedback": feedback, "fingerprint": source,
                              "source_operation": deepcopy(source_operation),
                              "source_check_results": deepcopy(task.get("check_results") or
                                  (task.get("replanning") or {}).get("source_check_results")),
                              "planning_model": settings["critical_plan_model"],
                              "planning_reasoning_effort": settings["critical_plan_reasoning_effort"],
                              "policy": deepcopy(policy)}
        if task.get("user_repair"):
            task["replanning"]["user_repair_epoch"] = task["user_repair"]["epoch"]
        state.update(phase="REPLAN", operation=None, last_error=None)
        if source_phase != "CODE":
            state["failures"] = 0
        state.pop("blocked_from", None)
        state.pop("provider_retry", None)
        return True

    def _retry_task_replanning(self, state):
        task = state.get("task") or {}
        if state.get("blocked_from") == "CODE":
            if not self._recover_code_timeout(state, self.runtime / "workspace"):
                raise ValueError("当前 CODE 阻塞没有可恢复的预算中断证据，或该中断已处理。")
            return
        if (state.get("blocked_from") != "REVIEW" or state.get("operation") is not None or
                task.get("kind") == "art" or task.get("reworks", -1) < self.config["max_reworks"] +
                task.get("manual_rework_allowance", 0) + task.get("autonomous_rework_allowance", 0)):
            raise ValueError("重新规划恢复仅允许当前 REVIEW 返工上限阻塞，不能替代其他恢复方式。")
        if not self._begin_task_replanning(state, state.get("last_error") or "返工窗口用完。", trigger="retry_replan"):
            raise ValueError("尚未启用 task_replanning。")

    def _replan(self, state, work):
        task = state["task"]
        pending = task.get("replanning") or {}
        if not self._task_replanning_policy(task)["enabled"]:
            self._block(state, "REPLAN", "同任务重新规划已停用；保留原断点，不调用规划模型。")
            return
        if self.config.get("planning_provider") != "claude":
            raise ProviderError("重新规划提供方必须保持 Claude，不能切换为审核者。")
        if pending.get("status") != "pending":
            self._block(state, "REPLAN", "重新规划已留下待处理结论；不能重复执行旧计划。")
            state["operation"] = None
            return
        source = pending["fingerprint"]
        if self._replanning_source(task, work) != source:
            raise ProviderError("重新规划来源已改变，不能继续旧计划或扩展返工窗口。")
        op = state.get("operation")
        if op and (op.get("planning_model") != pending["planning_model"] or
                   op.get("planning_reasoning_effort") != pending["planning_reasoning_effort"] or
                   op.get("replanning_fingerprint") != self._replanning_digest(source)):
            raise ProviderError("重新规划调用与已固定的模型或证据不匹配。")
        prompt = self._base_prompt() + (
            "\n你是当前功能任务的关键规划负责人。任务多次返工后仍未通过，请读取实际代码、历史交付、"
            "独立检查与最新审核，自主诊断失败原因并提出一个有限、可执行的同任务修复计划。"
            "调度器不提供候选实现，也不预判必须修改哪个系统。审核意见是待用证据验证的具体问题，"
            "不能把推测当成已证明的根因。只规划，不编辑、不运行命令、不提交。\n"
            "原任务范围、body、acceptance_criteria、core_tests、独立检查和 Codex 审核标准保持有效。"
            "不能通过删除失败断言、收窄原验收或把原目标降为人工待办来宣布完成。"
            "已有授权范围内的实现、参数、测试和证据问题应由你诊断后安排修复；"
            "只有确需外部权限、必要用户决策或无法获得资源时返回 blocked，reason 指明外部条件及事实证据。\n"
            "可修复时 action=task：title 为修复计划标题，body 写你的诊断、具体修复步骤及验证办法；"
            "acceptance_criteria 写本计划的补充验证步骤，无需回抄原任务的长篇验收文字。"
            "若 schema 要求 core_tests，可列出原核心测试。所有返回字段都只作修复计划，"
            "不会回写原任务标题、正文、验收或核心测试；原任务要求具有最高优先级。"
            "这只是同任务执行指导，不会创建新任务或替换原任务书。"
            "action=complete 不能代替原任务通过检查及独立审核。\n"
            + json.dumps({"task_id": task["id"], "head_sha": source["head_sha"],
                          "source_phase": pending["source_phase"],
                          "original_task": {key: task.get(key) for key in
                            ("title", "body", "acceptance_criteria", "core_tests", "base_sha")},
                          "latest_feedback": pending["feedback"], "source": source,
                          "previous_replanning": [{"result": row["result"], "head_sha": row["head_sha"]}
                                                   for row in task.get("replanning_history", [])]}, ensure_ascii=False))
        result = validate_output("plan", self._invoke(state, "plan", prompt, work))
        if self._replanning_source(task, work) != source:
            raise ProviderError("重新规划期间候选、范围或检查证据被改变，拒绝使用返回计划。")
        policy = self._task_replanning_policy(task)
        reason = None
        if not policy["enabled"]:
            reason = "同任务重新规划已停用；计划保留，未授予执行窗口。"
        elif result["action"] != "task":
            reason = ("规划完成不能代替原任务验收；保留原候选待处理。" if result["action"] == "complete"
                      else "Claude规划报告外部阻塞：" + result["reason"])
        elif len(self._epoch_replanning_history(task)) >= min(policy["max_cycles"], pending["policy"]["max_cycles"]):
            reason = "同任务重新规划周期已达到策略上限。"
        plan_hash = self._replanning_digest(result["body"].strip())
        if any(row.get("head_sha") == source["head_sha"] and row.get("plan_sha256") == plan_hash
               and (pending["source_phase"] != "CODE" or row.get("source_fingerprint", {}).get("wip_sha256") == source["wip_sha256"])
               for row in self._epoch_replanning_history(task)):
            reason = "同一 HEAD 与相同修复计划已处理，不能重复授予执行窗口。"
        op = state["operation"]
        path = _safe_under(self.runtime, Path("tasks") / task["id"] / f"replan-{pending['round']}.json")
        receipt = {"round": pending["round"], "task_id": task["id"], "head_sha": source["head_sha"],
                   "source_phase": pending["source_phase"],
                   "source_fingerprint": source, "output_dir": op["output_dir"], "result": result,
                   "planning_model": op["planning_model"], "planning_reasoning_effort": op["planning_reasoning_effort"],
                   "execution_decision": "blocked" if reason else "continue", "controller_reason": reason,
                   "plan_sha256": plan_hash, "receipt_path": str(path), "at": self.now().isoformat()}
        if task.get("user_repair"):
            if pending.get("user_repair_epoch") != task["user_repair"]["epoch"]:
                raise ProviderError("Repair planning belongs to another explicit authorization epoch")
            receipt["user_repair_epoch"] = task["user_repair"]["epoch"]
        if path.exists():
            existing = read_json(path)
            receipt["at"] = existing.get("at")
            if existing != receipt:
                raise ProviderError("已有同任务修复计划回执不匹配。")
        else:
            write_json(path, receipt)
        task.setdefault("replanning_history", []).append(receipt)
        pending.update(status="blocked" if reason else "applied", receipt_path=str(path))
        state["operation"] = None
        if reason:
            self._block(state, "REPLAN", reason)
            return
        # Counters are monotonic. A window begins only after a new, bound plan,
        # independently of the global/manual allowance and provider failures.
        limit = self.config["max_reworks"] + task.get("manual_rework_allowance", 0) + task.get("autonomous_rework_allowance", 0)
        window = min(policy["rework_window"], pending["policy"]["rework_window"])
        if pending["source_phase"] != "CODE":
            task["autonomous_rework_allowance"] = task.get("autonomous_rework_allowance", 0) + max(
                0, task["reworks"] + window - limit)
        task["feedback"] = ("原任务与原验收条件保持不变。以下为 Claude 关键规划模型的原文修复计划；"
                            "在既有授权内执行，完成后仍必须经过全部独立检查和 Codex 审核。\n"
                            + json.dumps(result, ensure_ascii=False) + "\n原始审核反馈：\n" + pending["feedback"])
        if pending["source_phase"] == "CODE":
            task["code_resume"] = True
        else:
            task["reworks"] += 1
            task["code_base"], task["code_resume"] = source["head_sha"], False
            task["check_results"] = []
        state.update(phase="CODE", failures=0, last_error=None)
        state.pop("blocked_from", None)
        state.pop("provider_retry", None)
