from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from .git_repository import GitRepository
from .prompts import execution_prompt, review_prompt
from .protocol import ProtocolError, commit_sha, parse_delivery, parse_handoff
from .storage import read_json, write_json, write_text


class RelayPaused(RuntimeError):
    pass


def config_binding(config: dict) -> str:
    identity = {
        "repository": config["repository"],
        "conversation_url": config["browser"].get("conversation_url"),
        "profile_dir": config["browser"].get("profile_dir"),
        "dsh_home": config["dsh"].get("home"),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class Relay:
    def __init__(self, config: dict, browser=None, runner=None, repository=None):
        self.config = config
        self.runtime = Path(config["runtime_dir"])
        self.state_path = self.runtime / "state.json"
        self.repo = repository or GitRepository(config["repository"])
        self.browser = browser
        self.runner = runner
        self.state = read_json(self.state_path) if self.state_path.exists() else {}
        if self.state and self.state.get("config_binding") != config_binding(config):
            raise RelayPaused("配置的项目或 GPT 对话已改变。请为新项目使用不同的 runtime_dir，避免串轮。")

    def save(self) -> None:
        self.state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_json(self.state_path, self.state)

    def seed(self, delivery: dict, replace: bool = False) -> None:
        if self.state and not replace:
            raise RelayPaused("已经有运行状态。继续请使用 run；确认恢复交付时才使用 seed --replace-state。")
        parsed = parse_delivery("【交付】" + json.dumps(delivery, ensure_ascii=False) + "【交付结束】")
        if delivery.get("base_sha"):
            parsed["base_sha"] = commit_sha(delivery["base_sha"])
        self.repo.ensure()
        self.repo.require_published(parsed["commit_sha"])
        self.state = {
            "version": 1, "config_binding": config_binding(self.config), "phase": "review_ready",
            "delivery": parsed, "completed_cycles": 0, "revisions": 0,
            "session_id": None, "history": [], "last_error": None,
        }
        self.save()

    def checkpoint(self) -> None:
        if (self.runtime / "PAUSE").exists():
            raise RelayPaused("检测到暂停标记。使用 resume 清除标记后再继续。")

    def prepare_attachments(self, turn_dir: Path) -> list[Path]:
        delivery = self.state["delivery"]
        sha = delivery["commit_sha"]
        self.repo.require_published(sha)
        options = self.config.get("evidence", {})
        reports, screenshots = delivery["report_paths"], delivery["screenshot_paths"]
        if options.get("require_reports", True) and not reports:
            raise RelayPaused("本次交付没有报告，无法开始审阅。请补充交付清单。")
        if options.get("require_screenshots", False) and not screenshots:
            raise RelayPaused("配置要求截图，但交付清单没有截图。")
        max_files = int(options.get("max_files", 20))
        max_bytes = int(options.get("max_total_mb", 40)) * 1024 * 1024
        attachments: list[Path] = []
        total = 0
        entries = [(p, False) for p in reports] + [(p, True) for p in screenshots]
        if len(entries) + 1 > max_files:
            raise RelayPaused("附件数量超过配置上限；不会静默丢弃证据。")
        turn_dir.mkdir(parents=True, exist_ok=True)
        for index, (relative, is_image) in enumerate(entries):
            data = self.repo.read_file(sha, relative)
            extension = Path(relative).suffix.lower()
            if is_image:
                supported = extension in (".png", ".jpg", ".jpeg", ".webp", ".gif")
                signature = data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
                if not supported or not signature:
                    raise RelayPaused(f"截图不是可上传的真实图片：{relative}")
            total += len(data)
            destination = turn_dir / f"{index + 1:02d}-{Path(relative).name}"
            destination.write_bytes(data)
            attachments.append(destination)
        base = delivery.get("base_sha")
        if base:
            if not self.repo.is_ancestor(base, sha):
                raise RelayPaused("交付 base_sha 不是交付提交的祖先。")
        elif not options.get("allow_last_commit_only", False):
            raise RelayPaused("首轮需要提供 base_sha，以便交给 GPT 完整代码差异；不能默认只审最后一次提交。")
        diff = self.repo.diff(base, sha)
        total += len(diff)
        if total > max_bytes:
            raise RelayPaused("证据和代码差异超过附件大小上限，请调整配置或交付范围。")
        patch = turn_dir / "code-changes.txt"
        patch.write_bytes(diff)
        attachments.append(patch)
        return attachments

    def _review(self, single_round: bool = False) -> None:
        pending = self.state["phase"] == "review_pending"
        if not pending:
            relay_id = "review-" + uuid.uuid4().hex
            turn_dir = self.runtime / "turns" / relay_id
            if self.config.get("evidence", {}).get("upload_attachments", False):
                attachments = self.prepare_attachments(turn_dir)
            else:
                self.repo.require_published(self.state["delivery"]["commit_sha"])
                base_sha = self.state["delivery"].get("base_sha")
                if base_sha and not self.repo.is_ancestor(base_sha, self.state["delivery"]["commit_sha"]):
                    raise RelayPaused("交付 base_sha 不是交付提交的祖先。")
                attachments = []
                turn_dir.mkdir(parents=True, exist_ok=True)
            baseline_sha = self.repo.fetch()
            prompt = review_prompt(self.config, self.state["delivery"], relay_id, baseline_sha, single_round=single_round)
            prompt_path = turn_dir / "review-prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            self.state["review"] = {
                "relay_id": relay_id, "turn_dir": str(turn_dir), "attachments": [str(p) for p in attachments],
                "baseline_sha": baseline_sha,
            }
            self.state["phase"] = "review_pending"
            self.save()  # durable send intent before any browser interaction
        review = self.state["review"]
        turn_dir = Path(review["turn_dir"])
        answer_path = turn_dir / "gpt-answer.txt"
        if answer_path.exists():
            answer = answer_path.read_text(encoding="utf-8")
        else:
            self.checkpoint()
            retry_confirmed = review.pop("retry_confirmed", False)
            if retry_confirmed:
                self.save()  # consume a one-shot, explicitly confirmed retry
            answer = self.browser.exchange(
                (turn_dir / "review-prompt.txt").read_text(encoding="utf-8"),
                [Path(p) for p in review["attachments"]], review["relay_id"], pending=pending and not retry_confirmed,
            )
            write_text(answer_path, answer)
        task = parse_handoff(answer, review["relay_id"], self.state["delivery"]["commit_sha"])
        if task["action"] in ("done", "pause"):
            self.state.update(phase="done" if task["action"] == "done" else "waiting_user", reason=task.get("reason", "GPT 要求暂停"))
            self.save()
            return
        revisions = self.state["revisions"] + 1 if task["action"] == "revise" else 0
        if task["action"] == "revise":
            if revisions > self.config["limits"]["max_revisions"]:
                raise RelayPaused("已达到单任务返工次数上限。")
        self.repo.verify_task(task, self.state["delivery"], review_base=review.get("baseline_sha"))
        task["run_id"] = "task-" + uuid.uuid4().hex
        self.state.update(phase="task_ready", task=task, revisions=revisions)
        self.save()

    def _execution(self) -> None:
        task = self.state["task"]
        result_path = self.runtime / "turns" / task["run_id"] / "result.json"
        if self.state["phase"] == "dsh_pending":
            if not result_path.exists():
                raise RelayPaused(
                    "上一轮 DSH 已经启动或可能启动，不能自动再次派发。"
                    "请检查 .relay/dsh 日志和远端提交，用 seed --delivery ... --replace-state 接回交付。"
                )
            result = read_json(result_path)
        else:
            self.checkpoint()
            # Revalidate task publication immediately before starting execution.
            self.repo.verify_task(task, self.state["delivery"], review_base=self.state["review"].get("baseline_sha"))
            synced_sha = self.repo.sync(task["published_sha"])
            if self.repo.read_file(synced_sha, task["task_path"]) != self.repo.read_file(task["published_sha"], task["task_path"]):
                raise RelayPaused("同步期间任务文件发生变化，不能派发未经本轮确认的任务。")
            prompt = execution_prompt(self.config, task, synced_sha)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            (result_path.parent / "dsh-prompt.txt").write_text(prompt, encoding="utf-8")
            self.state.update(phase="dsh_pending", execution_base_sha=synced_sha)
            self.save()  # durable execution intent; unknown runs never restart automatically
            outcome = self.runner.run(prompt, self.repo.path, task["run_id"], session_id=self.state.get("session_id"))
            result = {"session_id": outcome.session_id, "answer": outcome.answer, "exit_code": outcome.exit_code, "events_path": str(outcome.events_path)}
            write_json(result_path, result)
        delivery = parse_delivery(result["answer"], expected_task=task["task_id"])
        delivery["base_sha"] = self.state["execution_base_sha"]
        self.repo.verify_origin()
        self.repo.require_published(delivery["commit_sha"])
        if not self.repo.is_ancestor(delivery["base_sha"], delivery["commit_sha"]):
            raise RelayPaused("DSH 交付不包含已派发任务的基线版本。")
        if delivery["commit_sha"] == delivery["base_sha"]:
            raise RelayPaused("DSH 没有产生新的交付提交。")
        self.state["history"].append({"relay_id": self.state["review"]["relay_id"], "task_id": task["task_id"], "commit_sha": delivery["commit_sha"]})
        self.state["completed_cycles"] += 1
        self.state.update(phase="review_ready", delivery=delivery, session_id=result["session_id"], last_error=None)
        self.state.pop("review", None)
        self.state.pop("task", None)
        self.save()

    def run(self, once: bool = False) -> None:
        if not self.state:
            raise RelayPaused("请先使用 seed 登记当前 DSH 已完成的交付。")
        self.repo.ensure()
        started = self.state["completed_cycles"]
        try:
            while True:
                self.checkpoint()
                phase = self.state["phase"]
                print(f"[{time.strftime('%H:%M:%S')}] {phase} | 已完成 {self.state['completed_cycles']} 轮", flush=True)
                if phase in ("done", "waiting_user"):
                    print(self.state.get("reason", "已停止。"), flush=True)
                    return
                if self.state["completed_cycles"] >= self.config["limits"]["max_cycles"]:
                    raise RelayPaused("已达到 limits.max_cycles。调整配置后可继续，现有状态已保留。")
                if phase in ("review_ready", "review_pending"):
                    self._review(single_round=once)
                elif phase in ("task_ready", "dsh_pending"):
                    self._execution()
                    if once and self.state["completed_cycles"] > started:
                        return
                else:
                    raise RelayPaused(f"未知阶段：{phase}")
        except (Exception, KeyboardInterrupt) as error:
            self.state["last_error"] = str(error) or "用户中断。"
            self.save()
            raise

    def retry_review(self) -> None:
        if self.state.get("phase") != "review_pending":
            raise RelayPaused("当前不是待恢复的网页审阅阶段。")
        # Keep the same relay id: browser will still deduplicate if it is visible.
        turn_dir = Path(self.state["review"]["turn_dir"])
        if (turn_dir / "gpt-answer.txt").exists():
            raise RelayPaused("已保存 GPT 回复，不能标记为未发送。请核对回复或重新登记交付。")
        self.state["review"]["retry_confirmed"] = True
        self.save()

