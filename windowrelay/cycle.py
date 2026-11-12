"""Alternate GPT and DSH only after an explicit, stable completion response."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from relay.storage import read_json, run_lock, write_json


class AutoRelay:
    def __init__(self, config: dict, runtime: Path, sender: object, observer: object, clock=None):
        self.runtime = Path(runtime)
        self.state_path = self.runtime / "cycle-state.json"
        self.sender = sender
        self.observer = observer
        self.clock = clock if clock is not None else time.time
        if not callable(self.clock):
            raise ValueError("clock 必须是返回秒数的函数。")
        gpt, dsh = config.get("gpt", {}), config.get("dsh", {})
        self.binding = {
            "gpt_url": gpt.get("url", ""), "dsh_project": dsh.get("project", ""),
            "dsh_project_path": dsh.get("project_path", ""),
        }
        if any(not isinstance(value, str) for value in self.binding.values()):
            raise ValueError("GPT 地址、DSH 项目和路径必须为文本。")
        if not self.binding["gpt_url"].strip() or not self.binding["dsh_project"].strip():
            raise ValueError("请填写 gpt.url 和 dsh.project。")
        self.binding = {key: value.strip() for key, value in self.binding.items()}
        self.settings = config.get("auto", {})
        if not isinstance(self.settings, dict):
            raise ValueError("auto 配置必须为对象。")
        self.stable_samples = self._number(config, "stable_samples", 3, 1, 100, integer=True)
        self.settle_seconds = self._number(config, "settle_seconds", 5, 0, 3600)
        self.phase_timeout = self._number(config, "phase_timeout_seconds", 21600, 1, 604800)
        self.unavailable_timeout = self._number(config, "unavailable_timeout_seconds", 60, 1, 86400)
        self.ack_warn_seconds = self._number(config, "ack_warn_seconds", 60, 1, 86400)
        messages = config.get("messages", {})
        if not isinstance(messages, dict):
            raise ValueError("messages 配置必须为对象。")
        self.messages = {
            "gpt": messages.get("gpt", "DS做完了，请检查。"),
            "dsh": messages.get("dsh", "拉取最新的main，并完成指定任务书。"),
        }
        if any(not isinstance(message, str) or not message.strip() for message in self.messages.values()):
            raise ValueError("GPT 和 DSH 的固定消息必须是非空文本。")
        # A restart cannot prove what happened during the unobserved interval.
        self.session_id = str(uuid.uuid4())

    def _number(self, config, key, default, minimum, maximum, integer=False):
        value = self.settings.get(key, config.get(key, default))
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise ValueError(f"auto.{key} 必须在 {minimum} 到 {maximum} 之间。")
        if integer and int(value) != value:
            raise ValueError(f"auto.{key} 必须为整数。")
        return int(value) if integer else float(value)

    def _fresh_stability(self) -> dict:
        return {"fingerprint": None, "samples": 0, "since": None, "session_id": self.session_id}

    def _new_state(self, start_with: str, now: float) -> dict:
        return {
            "version": 1, "binding": dict(self.binding), "phase": "ready", "target": start_with,
            "phase_started_at": now, "unavailable_since": None, "pending": None, "current": None,
            "stability": self._fresh_stability(), "completed_turns": 0, "last_completed": None,
            "pause_reason": None, "last_resolution": None,
        }

    def _load(self, start_with="gpt") -> dict:
        if not self.state_path.exists():
            return self._new_state(start_with, self.clock())
        state = read_json(self.state_path)
        if state.get("version") != 1 or state.get("binding") != self.binding:
            raise ValueError("自动传话状态版本或 GPT/DSH 项目绑定不一致，请使用新的运行目录。")
        if state.get("phase") not in {"ready", "sending", "awaiting", "uncertain", "paused"}:
            raise ValueError("自动传话状态 phase 无效。")
        if state.get("target") not in {"gpt", "dsh"}:
            raise ValueError("自动传话状态 target 无效。")
        if state.get("pending") is not None:
            if not isinstance(state["pending"], dict):
                raise ValueError("自动传话 pending 无效。")
            state["phase"] = "uncertain"
        if state["phase"] == "awaiting" and not isinstance(state.get("current"), dict):
            raise ValueError("自动传话缺少本轮识别信息，禁止继续。")
        return state

    def _save(self, state: dict) -> None:
        state["updated_at"] = self.clock()
        write_json(self.state_path, state)

    def _prompt(self, target: str, token: str) -> str:
        if target == "gpt":
            condition = (
                "只有在完成本轮交付审阅，并已将一份明确可执行的下一任务书或修正任务书发布到项目仓库后，才算本轮完成。"
                "新任务书和修正任务书的标题文字及正文第一行，都必须直接以实际任务编号开头，例如 TASK-M2-20；不要添加 task+ 或其他额外前缀。"
                "发布任务书的 commit 标题必须仅为实际任务编号本身，例如 TASK-M2-20，不附带 task+、说明文字或其他前后缀。"
                "发布后在最终回复中另起一行，直接以实际编号开头标明下一任务，例如“TASK-M2-20 下一任务：具体内容”；修正任务也使用同一格式。"
                "示例只说明编号格式，必须使用实际任务编号，不得照抄示例编号；任务文件路径沿用项目规范。"
                "执行方拉取任务书后必须能立即开始；若没有下一任务、项目已经全部完成或需要人工决策，请报告阻塞。"
            )
        else:
            condition = (
                "只有在完成指定任务书要求的任务，并把本轮代码、报告和证据等交付全部上传到项目仓库后，才算本轮完成。"
                "请按任务书中的实际任务编号识别本轮任务；交付报告标题、正文第一行及最终回复中的本轮任务标识行，都必须直接以该实际任务编号开头，例如 TASK-M2-20；不要添加 task+ 或其他额外前缀。"
                "本轮所有提交的 commit 标题必须仅为实际任务编号本身，例如 TASK-M2-20，不附带 task+、说明文字或其他前后缀。"
                "示例只说明编号格式，必须使用实际任务编号；不要自行换编号或重复领取已完成任务。"
                "无法找到唯一明确的待执行任务书时，请报告阻塞，不要猜测。"
                "交付后等待下一张任务卡，不要自行开始下一任务。若任务书缺失、不明确或需要人工决策，请报告阻塞。"
            )
        prompt = (
            self.messages[target] + "\n\n【本轮自动交接约定】\n" + condition +
            "工具出错、等待用户批准或无法继续时，也必须报告阻塞，不能宣称完成。\n"
            f"本轮识别码：{token}\n"
            "完成后，把以下四段按顺序拼接为回复的独立最后一行：左双中括号、字面文本 RELAY_DONE:、"
            "本轮识别码、右双中括号。\n"
            "阻塞时用同样的四段拼接方法，把中间字面文本换为 RELAY_BLOCKED:，并在此前说明原因。\n"
            "只在最终完成或阻塞时输出相应标记一次；不要提前演示、引用、复述或在中途输出标记。"
        )
        if self._marker("DONE", token) in prompt or self._marker("BLOCKED", token) in prompt:
            raise ValueError("固定消息不能包含本轮完整交接标记。")
        return prompt

    @staticmethod
    def _marker(kind: str, token: str) -> str:
        return "[[RELAY_" + kind + ":" + token + "]]"

    @staticmethod
    def _field(observation, name, default=None):
        if isinstance(observation, dict):
            return observation.get(name, default)
        return getattr(observation, name, default)

    def _snapshot(self, target: str) -> dict:
        try:
            value = self.observer.snapshot(target)
        except Exception as error:
            return {"available": False, "reason": str(error) or type(error).__name__, "reason_code": "observer_error"}
        return {key: self._field(value, key, default) for key, default in (
            ("available", False), ("busy", None), ("idle", False), ("text", ""),
            ("fingerprint", ""), ("editable_text", ""), ("blocked", False), ("reason", ""),
            ("reason_code", ""), ("blocked_controls", ()), ("draft_present", False), ("editor_source", ""),
        )}

    @staticmethod
    def _diagnostic(observation: dict) -> dict:
        controls = observation.get("blocked_controls", ())
        if not isinstance(controls, (list, tuple)):
            controls = ()
        return {
            "observer_reason_code": observation.get("reason_code", "") if isinstance(observation.get("reason_code", ""), str) else "",
            "blocked_controls": [item for item in controls if isinstance(item, str)],
            "draft_present": observation.get("draft_present") is True,
            "editor_source": observation.get("editor_source", "") if isinstance(observation.get("editor_source", ""), str) else "",
        }

    def _result(self, state: dict, status: str, **extra) -> dict:
        action = state.get("current")
        if action is None and status == "completed":
            action = state.get("last_completed")
        action = action if isinstance(action, dict) else {}
        return {
            "status": status, "phase": state["phase"], "target": state["target"],
            "completed_turns": state["completed_turns"], "server_received": None,
            "request_status": "ui_echo_observed" if action.get("ui_request_echo_seen_at") is not None else "not_yet_observed",
            "response_seen": action.get("response_seen_at") is not None, **extra,
        }

    def _record_request_observations(self, current: dict | None, observation: dict, now: float) -> None:
        if current is None or observation.get("available") is not True:
            return
        if observation.get("busy") is True and current.get("busy_seen_at") is None:
            current["busy_seen_at"] = now
        text, token = observation.get("text"), current.get("token")
        if not isinstance(text, str) or not isinstance(token, str) or not token:
            return
        editable = observation.get("editable_text")
        if (observation.get("draft_present") is not True and isinstance(editable, str)
                and not editable.strip() and token in text and current.get("ui_request_echo_seen_at") is None):
            # This proves only that UI text contains the nonce, not backend receipt.
            current["ui_request_echo_seen_at"] = now
        for kind in ("DONE", "BLOCKED"):
            marker = self._marker(kind, token)
            if marker not in current.get("message", "") and any(line.strip() == marker for line in text.splitlines()):
                if current.get("response_seen_at") is None:
                    current["response_seen_at"] = now
                current["response_evidence"] = kind.lower() + "_marker"

    def _pause(self, state: dict, code: str, reason: str, observation_reason: str = "", diagnostic: dict | None = None) -> dict:
        if state["phase"] != "paused":
            state["paused_from"] = state["phase"]
            state["paused_at"] = self.clock()
        observation_reason = observation_reason if isinstance(observation_reason, str) else ""
        if observation_reason:
            reason += " 窗口读取信息：" + observation_reason
        diagnostic = diagnostic or {}
        state.update(phase="paused", pause_reason=reason, pause_code=code,
                     pause_observer_reason=observation_reason, pause_diagnostic=diagnostic)
        state["stability"] = self._fresh_stability()
        self._save(state)
        return self._result(state, "paused", reason=reason, reason_code=code,
                            observer_reason=observation_reason, **diagnostic)

    def _wait(self, state: dict, reason: str, code: str, observation_reason: str = "", diagnostic: dict | None = None) -> dict:
        observation_reason = observation_reason if isinstance(observation_reason, str) else ""
        if observation_reason:
            reason += " 窗口读取信息：" + observation_reason
        self._save(state)
        return self._result(state, "waiting", reason=reason, reason_code=code,
                            observer_reason=observation_reason, stable_samples=state["stability"]["samples"],
                            **(diagnostic or {}))

    def step(self, execute: bool = False, start_with: str = "gpt") -> dict:
        if not isinstance(execute, bool) or start_with not in {"gpt", "dsh"}:
            raise ValueError("execute 必须为布尔值，start_with 必须为 gpt 或 dsh。")
        if not execute:
            state = self._load(start_with)
            result = self._result(state, "dry_run", paused=(self.runtime / "PAUSE").exists())
            if state["phase"] == "ready":
                result.update(action="would_prepare", message=self._prompt(state["target"], uuid.uuid4().hex))
            else:
                result.update(action="observe_only", pending=state.get("pending"), reason=state.get("pause_reason"))
            return result
        with run_lock(self.runtime / ".cycle-step-lock"):
            state = self._load(start_with)
            if state.get("pending") is not None or state["phase"] == "uncertain":
                state["phase"] = "uncertain"
                self._save(state)
                return self._result(state, "uncertain", pending=state.get("pending"),
                                    reason="上轮发送结果待人工确认，禁止自动重发。")
            if state["phase"] == "paused":
                return self._result(state, "paused", reason=state.get("pause_reason"),
                                    reason_code=state.get("pause_code"),
                                    observer_reason=state.get("pause_observer_reason", ""),
                                    **state.get("pause_diagnostic", {}))
            if (self.runtime / "PAUSE").exists():
                return self._pause(state, "pause_requested", "检测到暂停标记，已停止自动交接。")
            now = self.clock()
            if now - state["phase_started_at"] >= self.phase_timeout:
                return self._pause(state, "phase_timeout", "本轮等待超过时限，未自动交给另一方。")
            if state.get("stability", {}).get("session_id") != self.session_id:
                state["stability"] = self._fresh_stability()
            observation = self._snapshot(state["target"])
            observation_reason = observation.get("reason", "")
            diagnostic = self._diagnostic(observation)
            current = state.get("current")
            self._record_request_observations(current, observation, now)
            if observation.get("blocked") is True:
                return self._pause(state, "approval_required", "当前窗口报告待审批，需要人工处理。", observation_reason, diagnostic)
            if observation.get("available") is not True:
                state["stability"] = self._fresh_stability()
                if state.get("unavailable_since") is None:
                    state["unavailable_since"] = now
                if now - state["unavailable_since"] >= self.unavailable_timeout:
                    return self._pause(state, "unavailable_timeout", "无法读取当前窗口，等待超过时限。", observation_reason, diagnostic)
                return self._wait(state, "当前窗口不可读取，继续等待。", "unavailable", observation_reason, diagnostic)
            state["unavailable_since"] = None
            text = observation.get("text")
            if not isinstance(text, str):
                text = ""
            if current and self._marker("BLOCKED", current["token"]) in text:
                return self._pause(state, "blocked_marker", "本轮回复报告阻塞，未自动交接。", observation_reason, diagnostic)
            condition = None
            if observation.get("busy") is True:
                if current and current.get("ui_request_echo_seen_at") is None:
                    condition = ("当前目标显示处理中，但尚未观察到本轮识别码回显；请核对是否收到本轮请求。", "busy_unlinked")
                else:
                    condition = ("当前目标仍在处理任务。", "busy")
            elif observation.get("busy") is not False:
                condition = ("无法确认目标是否仍在处理任务。", "busy_unknown")
            elif observation.get("draft_present") is True or not isinstance(observation.get("editable_text"), str) or observation["editable_text"].strip():
                condition = ("输入框有草稿或无法读取，请人工处理。", "draft_present")
            elif observation.get("idle") is not True:
                condition = ("尚未确认目标已空闲。", "not_idle")
            if condition:
                state["stability"] = self._fresh_stability()
                return self._wait(state, *condition, observation_reason=observation_reason, diagnostic=diagnostic)
            if state["phase"] == "ready":
                target = state["target"]
                self.sender.validate(target)
                if (self.runtime / "PAUSE").exists():
                    return self._pause(state, "pause_requested", "检测到暂停标记，未输入消息。")
                token = uuid.uuid4().hex
                action = {
                    "event_id": str(uuid.uuid4()), "token": token, "target": target,
                    "message": self._prompt(target, token), "created_at": now,
                    "status": "pending", "server_received": None,
                    "ui_request_echo_seen_at": None, "busy_seen_at": None,
                    "response_seen_at": None, "response_evidence": None,
                }
                state.update(phase="sending", pending=action, current=action)
                self._save(state)
                try:
                    getattr(self.sender, f"send_{target}")(action["message"], action["event_id"])
                except BaseException as error:
                    action.update(status="uncertain", error=str(error) or type(error).__name__)
                    state["phase"] = "uncertain"
                    self._save(state)
                    if not isinstance(error, Exception):
                        raise
                    return self._result(state, "uncertain", pending=action, reason="发送结果不确定，禁止自动重发。")
                action.update(status="send_action_completed", send_action_completed_at=self.clock())
                state.update(phase="awaiting", pending=None, phase_started_at=self.clock(), unavailable_since=None)
                state["stability"] = self._fresh_stability()
                self._save(state)
                return self._result(state, "awaiting", event_id=action["event_id"], reason="已完成发送动作，等待本轮完成回复。")
            marker = self._marker("DONE", current["token"])
            # The nonce is unique to this turn. UIA may expose the same response in
            # a Document TextPattern and again in child Text names; that is not a
            # second response. Require a standalone line, rather than a tree count.
            exact_line = any(line.strip() == marker for line in text.splitlines())
            if not exact_line or marker in current["message"]:
                state["stability"] = self._fresh_stability()
                if current.get("ui_request_echo_seen_at") is None and now - state["phase_started_at"] >= self.ack_warn_seconds:
                    return self._wait(state, "尚未观察到本轮请求的界面回显，请核对目标对话是否收到消息；脚本不会自动重发或交给另一方。",
                                      "request_not_confirmed", observation_reason, diagnostic)
                return self._wait(state, "尚未看到与本轮识别码匹配且独立成行的完成标记。", "completion_missing", observation_reason, diagnostic)
            fingerprint = observation.get("fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                state["stability"] = self._fresh_stability()
                return self._wait(state, "无法确认回复内容是否稳定。", "fingerprint_missing", observation_reason, diagnostic)
            stable = state["stability"]
            if stable["fingerprint"] != fingerprint:
                stable.update(fingerprint=fingerprint, samples=1, since=now)
            else:
                stable["samples"] += 1
            if stable["samples"] < self.stable_samples or now - stable["since"] < self.settle_seconds:
                return self._wait(state, "本轮完成标记已出现，等待连续稳定确认。", "settling", observation_reason, diagnostic)
            completed = {**current, "completed_at": now, "fingerprint": fingerprint}
            previous_target = state["target"]
            next_target = "dsh" if previous_target == "gpt" else "gpt"
            state.update(
                phase="ready", target=next_target, phase_started_at=now, unavailable_since=None,
                current=None, pending=None, last_completed=completed,
                completed_turns=state["completed_turns"] + 1,
            )
            state["stability"] = self._fresh_stability()
            self._save(state)
            return self._result(state, "completed", completed_target=previous_target, next_target=next_target,
                                reason="本轮回复已通过标记和稳定确认；下一次检查才准备另一方。")

    def status(self) -> dict:
        state = self._load() if self.state_path.exists() else None
        return {"runtime_dir": str(self.runtime), "paused": (self.runtime / "PAUSE").exists(), "state": state}

    def resolve(self, sent: bool) -> dict:
        if not isinstance(sent, bool):
            raise ValueError("sent 必须明确为 True（人工确认已发送）或 False（跳过并暂停）。")
        with run_lock(self.runtime / ".cycle-step-lock"):
            state = self._load()
            pending = state.get("pending")
            if not pending:
                return self._result(state, "no_pending")
            now = self.clock()
            state["last_resolution"] = {"event_id": pending["event_id"], "sent": sent, "at": now}
            state.update(pending=None, current=pending, target=pending["target"], unavailable_since=None)
            state["stability"] = self._fresh_stability()
            if not sent:
                return self._pause(state, "skipped_uncertain", "本轮不确定发送已跳过；请人工复核并重置后再运行。")
            pending.update(status="human_confirmed_sent", human_confirmed_at=now)
            state.update(phase="awaiting", phase_started_at=now, pause_reason=None)
            self._save(state)
            return self._result(state, "awaiting", resolved=True, reason="已人工确认发送，继续等待原目标本轮回复。")

    def reset(self, start_with: str = "gpt") -> dict:
        if start_with not in {"gpt", "dsh"}:
            raise ValueError("start_with 必须为 gpt 或 dsh。")
        with run_lock(self.runtime / ".cycle-step-lock"):
            previous = self._load(start_with)
            if previous.get("pending") is not None or previous["phase"] == "uncertain":
                raise ValueError("存在不确定发送，请先人工核对并 resolve，再重置循环。")
            now = self.clock()
            state = self._new_state(start_with, now)
            state["last_reset"] = {
                "previous_phase": previous["phase"], "previous_target": previous["target"],
                "previous_token": (previous.get("current") or {}).get("token"),
                "previous_completed_turns": previous["completed_turns"], "at": now,
            }
            self._save(state)
            return self._result(state, "ready", reset=True,
                                reason="已人工重置；本次没有发送消息，下一次检查才准备目标。")

    def resume(self) -> dict:
        """Continue a user pause without issuing another send or replacing its token."""
        with run_lock(self.runtime / ".cycle-step-lock"):
            state = self._load()
            if state.get("pending") is not None or state["phase"] == "uncertain":
                raise ValueError("存在不确定发送，请先人工核对并 resolve，不能直接恢复。")
            if state["phase"] != "paused":
                return self._result(state, "resumed", noop=True)
            if state.get("pause_code") != "pause_requested":
                raise ValueError("当前因故障或阻塞暂停，请人工处理后使用 reset-cycle；不能直接 resume。")
            now = self.clock()
            paused_for = max(0, now - state.get("paused_at", now))
            state["phase_started_at"] += paused_for
            if state.get("unavailable_since") is not None:
                state["unavailable_since"] += paused_for
            previous_phase = state.get("paused_from")
            state.update(
                phase="awaiting" if state.get("current") is not None else "ready",
                pause_reason=None, pause_code=None,
                last_resume={"at": now, "paused_seconds": paused_for, "paused_from": previous_phase},
            )
            state["stability"] = self._fresh_stability()
            self._save(state)
            return self._result(state, "resumed", noop=False,
                                reason="已恢复原轮次；未重新发送消息，继续等待原目标。")
