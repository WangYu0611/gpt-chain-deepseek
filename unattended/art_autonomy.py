"""Controller boundaries for Claude's own art diagnosis and recovery plans.

This module dispatches and validates evidence. It contains no modeling strategy.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from . import gitops
from .storage import lock, read_json, write_json, write_text


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or getattr(path.stat(), "st_file_attributes", 0) & 0x400:
        raise RuntimeError("美术诊断输入不能为链接或非文件。")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ArtAutonomy:
    def _art_response_queue(self):
        from .ue_checks import _safe_under
        path = _safe_under(self.runtime, "art-diagnosis-responses.json")
        if not path.exists():
            return path, {"version": 1, "responses": []}
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("美术诊断回答队列不可读或过大。")
        queue = read_json(path)
        if (queue.get("version") != 1 or not isinstance(queue.get("responses"), list)
                or len(queue["responses"]) > 500 or any(not isinstance(row, dict) for row in queue["responses"])):
            raise ValueError("美术诊断回答队列格式无效。")
        return path, queue

    def _art_waiting_diagnosis(self, state, task_id, receipt_sha256):
        """Bind a response to the exact still-current question and its inputs."""
        from .ue_checks import _safe_under
        task = state.get("task") or {}
        pending = task.get("art_diagnosis") or {}
        if (state.get("phase") != "BLOCKED" or state.get("blocked_from") != "ART_DIAGNOSE"
                or state.get("operation") is not None or task.get("kind") != "art"
                or task.get("id") != task_id or pending.get("status") != "waiting_user"
                or not self._art_autonomy_policy()["enabled"]):
            raise ValueError("该美术诊断已不是当前等待回答的问题。")
        number = pending.get("round")
        if type(number) is not int or number < 1:
            raise ValueError("美术诊断轮次无效。")
        path = _safe_under(self.runtime, Path("tasks") / task_id / f"diagnosis-{number}.json")
        if (Path(pending.get("receipt_path", "")).resolve() != path.resolve()
                or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024
                or file_digest(path) != receipt_sha256):
            raise ValueError("美术诊断回执已变化，请刷新后回答当前问题。")
        receipt = read_json(path)
        source = pending.get("fingerprint")
        history = task.get("art_diagnosis_history", [])
        if (not isinstance(source, dict) or receipt.get("task_id") != task_id
                or receipt.get("round") != number or receipt.get("source_fingerprint") != source
                or receipt.get("head_sha") != source.get("head_sha")
                or receipt.get("execution_decision") != "waiting"
                or receipt.get("art_autonomy_epoch", 0) != task.get("art_autonomy_epoch", 0)
                or not history or history[-1] != receipt
                or self._art_diagnosis_source(task, self.runtime / "workspace") != source):
            raise ValueError("美术诊断来源已变化，不能将回答应用到旧问题。")
        return receipt

    @staticmethod
    def _art_response_result(row, *, idempotent):
        return {"status": row["status"], "response_id": row["id"], "task_id": row["task_id"],
                "receipt_sha256": row["receipt_sha256"], "idempotent": idempotent}

    def respond_art_diagnosis(self, task_id, receipt_sha256, feedback):
        """Enqueue a real user answer without acquiring the running controller lock."""
        if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", task_id)
                or not isinstance(receipt_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256)
                or not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 16000
                or "\x00" in feedback):
            raise ValueError("需要有效的任务编号、当前诊断 SHA256 和非空用户原话（最多16000字）。")
        identity = digest({"task_id": task_id, "receipt_sha256": receipt_sha256, "feedback": feedback})
        with lock(self.runtime / "art-queue.lock"):
            path, queue = self._art_response_queue()
            state = self._load()
            task = state.get("task") or {}
            existing = next((row for row in queue["responses"] if row.get("id") == identity), None)
            applied = task.get("art_diagnosis_responses", [])
            if (existing and existing.get("status") in {"queued", "consumed"}
                    and task.get("id") == task_id and any(row.get("id") == identity for row in applied)):
                # State is authoritative if a previous consume crashed before its acknowledgement.
                return self._art_response_result({**existing, "status": "consumed"}, idempotent=True)
            receipt = self._art_waiting_diagnosis(state, task_id, receipt_sha256)
            if existing and existing.get("status") == "queued":
                return self._art_response_result(existing, idempotent=True)
            if any(row.get("task_id") == task_id and row.get("receipt_sha256") == receipt_sha256
                   for row in queue["responses"]):
                raise ValueError("该诊断已有回答；不能覆盖或重复授权，请等待后台处理。")
            if len(queue["responses"]) >= 500:
                raise ValueError("美术诊断回答队列已达保存上限。")
            if self._load() != state:
                raise ValueError("美术状态刚发生变化，请刷新后重试。")
            row = {"id": identity, "task_id": task_id, "receipt_sha256": receipt_sha256,
                   "diagnosis_round": receipt["round"], "source_fingerprint": receipt["source_fingerprint"],
                   "feedback": feedback, "created_at": self.now().isoformat(), "status": "queued"}
            queue["responses"].append(row)
            write_json(path, queue)
            return self._art_response_result(row, idempotent=False)

    def _sync_art_diagnosis_documents(self, task):
        from .ue_checks import _safe_under
        directory = _safe_under(self.runtime, Path("tasks") / task["id"])
        write_json(directory / "task.json", task)
        write_text(directory / "TASK.md", task["id"] + " " + task["title"] + "\n\n" + task["body"] +
                   "\n\n执行：Claude Opus 5.5 / high。资产生产不得更改主项目源码或提交。\n")
        queue = self.art_queue()
        entry = next((row for row in queue["requests"] if row.get("id") == task["id"]), None)
        if entry is not None:
            entry["body"] = task["body"]
            write_json(self.runtime / "art-queue.json", queue)

    def consume_art_diagnosis_response(self, state):
        """Called under the controller lock; save the new epoch before acknowledging.

        Answers cause a fresh read-only author diagnosis, never acceptance of the
        previous plan. A stale queued answer is rejected without changing state.
        """
        task = state.get("task") or {}
        if (task.get("kind") != "art" or not (self.runtime / "art-diagnosis-responses.json").exists()
                or (self.runtime / "PAUSE").exists()):
            return False
        with ExitStack() as stack:
            try:
                stack.enter_context(lock(self.runtime / "art-queue.lock"))
            except (RuntimeError, OSError):
                # A simultaneous Web submission owns this short-lived inbox
                # lock. Leave it for the next poll instead of stopping the worker.
                return False
            path, queue = self._art_response_queue()
            rows = [row for row in queue["responses"] if row.get("task_id") == task["id"]
                    and row.get("status") == "queued"]
            if not rows:
                return False
            for row in rows:
                applied = task.get("art_diagnosis_responses", [])
                if not any(item.get("id") == row.get("id") for item in applied):
                    try:
                        receipt = self._art_waiting_diagnosis(state, task["id"], row["receipt_sha256"])
                        if (receipt["source_fingerprint"] != row.get("source_fingerprint")
                                or receipt["round"] != row.get("diagnosis_round")
                                or row.get("id") != digest({key: row[key] for key in ("task_id", "receipt_sha256", "feedback")})):
                            raise ValueError("排队回答与诊断来源不一致。")
                    except (ValueError, RuntimeError, KeyError) as error:
                        row.update(status="rejected", error=str(error), rejected_at=self.now().isoformat())
                        write_json(path, queue)
                        continue
                    updated = deepcopy(state)
                    next_task = updated["task"]
                    epoch = next_task.get("art_autonomy_epoch", 0) + 1
                    context = (f"\n\n用户对诊断 #{receipt['round']} 的原话（回答编号 {row['id']}）：\n"
                               + row["feedback"] + "\n\n此回答交由 Claude 重新诊断和提案；不自动批准旧计划、"
                               "更改可信配置或预算，也不代表资产视觉验收通过。")
                    next_task["body"] += context
                    next_task["art_request"]["body"] = next_task["body"]
                    next_task["feedback"] = context.lstrip()
                    next_task["art_autonomy_epoch"] = epoch
                    next_task.setdefault("art_diagnosis_responses", []).append(
                        {"id": row["id"], "receipt_sha256": row["receipt_sha256"], "feedback": row["feedback"],
                         "art_autonomy_epoch": epoch, "applied_at": self.now().isoformat()})
                    if not self._begin_art_diagnosis(updated, next_task["feedback"], trigger="user_response"):
                        raise RuntimeError("美术自主诊断已停用。")
                    self._save(updated)
                    state.clear()
                    state.update(updated)
                    task = state["task"]
                self._sync_art_diagnosis_documents(task)
                row.update(status="consumed", consumed_at=self.now().isoformat(),
                           art_autonomy_epoch=task.get("art_autonomy_epoch", 0))
                write_json(path, queue)
            return True

    def _art_autonomy_policy(self):
        from .config import validate_art_autonomy_settings
        value = self.config.get("art_autonomy")
        return validate_art_autonomy_settings(value) if value else {"enabled": False, "max_cycles": 1, "rework_window": 1}

    def _art_diagnosis_source(self, task, work):
        from .art_tripo import TripoClient
        self._validate_art_task(task, work)
        cfg, paths = self._art_files(task, work)
        # The controller reads only local accounting. No credentials or private
        # ledger contents are handed to the model or published in its receipt.
        client = TripoClient(cfg["private_state_dir"], cfg["key_file"], cfg["budget_limit"])
        with client._locked():
            budget = client._budget(client._load())
        public = Path(task["art_request"]["artifacts_dir"])
        directory = self.runtime / "tasks" / task["id"]
        round_ = task["reworks"]
        return {"head_sha": gitops.head(work),
                "config_sha256": file_digest(task["art_request"]["config_file"]),
                "editable_files": {name: file_digest(path) for name, path in paths.items()},
                "references": [{"sha256": file_digest(item["path"]), "approved_sha256": item["sha256"]}
                               for item in cfg.get("references", [])],
                "review_package_sha256": file_digest(public / "review-package.json"),
                "independent_check_sha256": file_digest(public / "independent-check.json"),
                "review_sha256": file_digest(directory / f"review-{round_}.json"),
                "delivery_sha256": file_digest(directory / f"delivery-{round_}.md"),
                "scope_sha256": digest({"body": task["body"], "art_request": task["art_request"]}),
                "budget": budget}

    def _begin_art_diagnosis(self, state, feedback, *, trigger):
        policy = self._art_autonomy_policy()
        if not policy["enabled"]:
            return False
        task = state["task"]
        history = task.get("art_diagnosis_history", [])
        epoch = task.get("art_autonomy_epoch", 0)
        executed = sum(row.get("execution_decision") == "continue" and row.get("art_autonomy_epoch", 0) == epoch
                       for row in history)
        if executed >= policy["max_cycles"]:
            self._block(state, state["phase"], "美术自主诊断窗口已用完，需处理持续停滞。" + feedback)
            state["operation"] = None
            return True
        source = self._art_diagnosis_source(task, self.runtime / "workspace")
        number = max((row.get("round", index + 1) for index, row in enumerate(history)), default=0) + 1
        task["art_diagnosis"] = {"round": number, "art_autonomy_epoch": epoch, "trigger": trigger,
                                 "source_phase": state["phase"], "feedback": feedback,
                                 "fingerprint": source, "source_operation": deepcopy(state.get("operation")),
                                 "status": "pending"}
        state.update(phase="ART_DIAGNOSE", operation=None, failures=0, last_error=None)
        state.pop("blocked_from", None)
        state.pop("provider_retry", None)
        return True

    def _art_diagnose(self, state, work):
        from .providers import validate_output
        task = state["task"]
        pending = task["art_diagnosis"]
        if pending.get("status") != "pending":
            self._block(state, "ART_DIAGNOSE", "Claude诊断仍待必要决策或外部条件；不能重复执行旧计划。")
            state["operation"] = None
            return
        source = pending["fingerprint"]
        if self._art_diagnosis_source(task, work) != source:
            raise RuntimeError("美术诊断来源已改变，不能复用旧计划。")
        if any(item["sha256"] != item["approved_sha256"] for item in source["references"]):
            raise RuntimeError("已批准风格参考文件的摘要不匹配。")
        prompt = (
            "你是本美术任务的实际制作负责人。请依据自己的制作记录和本轮证据，独立诊断为何未完成，"
            "自主提出下一步计划。调度器不提供候选美术方案，由你决定方法。\n"
            "此调用为原制作会话的只读分支，只分析和回答，不制作、不执行命令、不调用付费API。"
            "结构化返回action、diagnosis、plan、reason、required_approvals、questions、"
            "additional_tripo_credits、head_sha。\n"
            "已确认风格、批次范围、预算上限、工具权限和用户视觉验收边界保持有效。"
            "这些边界以内的制作方法和技术选择由你自主决定，无须逐项向用户请示。"
            "区分用户明确要求、你过去的自主选型和审核者建议；不要把自己的旧选型变成用户硬要求。"
            "若可在现有授权内处理，action=continue，写出你自己的具体执行计划，required_approvals和questions为空。"
            "只有确需改变既有授权边界时action=needs_user，明确所需批准类别与简短问题；"
            "确实缺少不可获得的外部条件才action=blocked，并说明原因。"
            "预计新增Tripo积分按本计划填写，未知费用不可假称0；不能估计时先解释缺失条件。"
            "不要宣布作品已被用户接受。\n"
            + json.dumps({"task_id": task["id"], "task": task["body"], "feedback": pending["feedback"],
                          "trigger": pending["trigger"], "source": source,
                          "public_artifacts": task["art_request"]["artifacts_dir"],
                          "task_documents": str(self.runtime / "tasks" / task["id"])}, ensure_ascii=False))
        result = validate_output("art_diagnose", self._invoke(state, "art_diagnose", prompt, work,
                                                             providers=self._art_providers(task)))
        if result["head_sha"] != source["head_sha"] or self._art_diagnosis_source(task, work) != source:
            raise RuntimeError("美术诊断回执与当前证据不匹配。")
        output = Path(state["operation"]["output_dir"])
        number = pending["round"]
        source_hash = digest(source)
        receipt_path = self.runtime / "tasks" / task["id"] / f"diagnosis-{number}.json"
        public_path = Path(task["art_request"]["artifacts_dir"]) / "diagnoses" / f"{number}-{source_hash}.json"
        epoch = pending.get("art_autonomy_epoch", 0)
        receipt = {"task_id": task["id"], "head_sha": source["head_sha"], "round": number, "art_autonomy_epoch": epoch,
                   "trigger": pending["trigger"], "source_fingerprint": source,
                   "output_dir": str(output), "result": result, "at": self.now().isoformat(),
                   "receipt_path": str(receipt_path), "public_receipt": str(public_path)}
        identity_path = output / "model-selection.json"
        if identity_path.is_file():
            receipt["model_selection"] = read_json(identity_path)
        policy = self._art_autonomy_policy()
        budget = source["budget"]
        reason = None
        if not policy["enabled"]:
            reason = "美术自主执行已停用，计划保留待处理。"
        elif result["action"] != "continue" or result["required_approvals"] or result["questions"]:
            reason = "Claude需要确认：" + ("；".join(result["questions"]) or result["reason"])
        elif budget["breached"] or result["additional_tripo_credits"] > budget["remaining"]:
            reason = "Claude方案预计积分超过当前可用预算，需要调整预算或方案。"
        progress = digest({key: source[key] for key in ("editable_files", "review_package_sha256")})
        plan_hash = digest(result["plan"].strip())
        if any(row.get("progress_fingerprint") == progress and row.get("plan_sha256") == plan_hash
               and row.get("execution_decision") == "continue" and row.get("art_autonomy_epoch", 0) == epoch
               for row in task.get("art_diagnosis_history", [])):
            reason = "同一产物与同一Claude计划已执行过，未见新进展，保留诊断待处理。"
        receipt.update(progress_fingerprint=progress, plan_sha256=plan_hash,
                       execution_decision="waiting" if reason else "continue", controller_reason=reason)
        # Persist the exact author plan before granting another production
        # window. Recovery uses the same operation receipt and never calls it twice.
        if receipt_path.exists():
            existing = read_json(receipt_path)
            receipt["at"] = existing.get("at")
            if existing != receipt:
                raise RuntimeError("已有美术诊断回执内容冲突。")
        else:
            write_json(receipt_path, receipt)
        if public_path.exists():
            if read_json(public_path) != receipt:
                raise RuntimeError("公开美术诊断回执内容冲突。")
        else:
            write_json(public_path, receipt)
        task.setdefault("art_diagnosis_history", []).append(receipt)
        pending.update(status="waiting_user" if reason else "applied", receipt_path=str(receipt_path))
        if reason:
            state["operation"] = None
            self._block(state, "ART_DIAGNOSE", reason)
            return
        feedback = ("Claude自主诊断后继续制作。以下是你自己的原文计划；在现有授权和固定入口内自主执行，"
                    "完成后仍提交独立检查和用户视觉验收。\n" + result["diagnosis"] + "\n" + result["plan"])
        current_limit = (self.config["max_reworks"] + task.get("manual_rework_allowance", 0)
                         + task.get("autonomous_rework_allowance", 0))
        task["autonomous_rework_allowance"] = task.get("autonomous_rework_allowance", 0) + max(
            0, task["reworks"] + policy["rework_window"] - current_limit)
        self._art_rework(state, feedback)
