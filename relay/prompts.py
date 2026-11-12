from __future__ import annotations

import json


def review_prompt(config: dict, delivery: dict, relay_id: str, baseline_sha: str | None = None, single_round: bool = False) -> str:
    sample = {
        "relay_id": relay_id, "reviewed_sha": delivery["commit_sha"],
        "action": "execute", "task_id": "下一任务编号", "task_path": "Docs/Tasks/下一任务.md",
        "published_sha": "任务发布后的完整提交SHA", "reason": "验收结论",
    }
    authorization = "用户本次明确要求先跑通一轮：审阅当前交付并发布一个后续或返工任务，DSH 完成这一个任务后停止。这是当前独立传话器的单次试跑，不授权开启旧 hosted automation、旧执行器或连续开发。" if single_round else "按本次配置的循环范围执行，遇到需要用户决定的事项请暂停。"
    evidence_source = (
        "本消息附件包含报告、代码差异和可用截图；完整代码请通过已连接的 GitHub 工具读取上述提交。"
        if config.get("evidence", {}).get("upload_attachments", False)
        else "请直接使用已连接的 GitHub 工具，从上述固定提交读取任务书、报告、证据索引、原始日志、截图及源码，并对照任务开始前的版本审阅代码变化。本消息不上传文件。"
    )
    return f"""[AUTO-RELAY:{relay_id}]
{authorization}
DSH 已完成任务 {delivery['task_id']} 并提交到 {config['repository']['url']} 的 {config['repository']['branch']} 分支。
请按本对话已有流程独立审阅，不要运行新的开发任务。
固定审阅提交：{delivery['commit_sha']}
本轮通知前远端版本：{baseline_sha or '以本轮实际远端版本为准'}。本轮发布的任务文件须相对这个版本新增或更新，并取得新的发布 SHA。
报告：{json.dumps(delivery['report_paths'], ensure_ascii=False)}
截图：{json.dumps(delivery['screenshot_paths'], ensure_ascii=False)}
{evidence_source}原日志可能有 UTF-16 或 Git 换行归一化，请依据证据索引辨别原字节哈希和 Git blob，不要混用。
缺少截图、测试未运行、证据不完整或无法读取仓库时，请如实说明，不能视为已验证；无法取得必要证据时 action=pause。
若通过，在同一分支发布下一任务；若需返工，在同一分支发布或更新返工任务文件。
任务写入 GitHub 成功后再返回 execute 或 revise，不能仅在聊天中起草。请沿用项目任务规范；工具不要求改用 PR。
如果项目完成，action=done；需要用户决定或缺少能力，action=pause，不要虚构发布成功。
最后单独输出下面格式的 JSON 交接块。action 仅允许 execute/revise/pause/done；pause/done 可省略任务与发布字段。
【交接】
{json.dumps(sample, ensure_ascii=False, indent=2)}
【交接结束】
本轮传话编号 {relay_id}。请只处理这一轮交付，发布一次。
"""


def execution_prompt(config: dict, task: dict, synced_sha: str) -> str:
    sample = {"task_id": task["task_id"], "commit_sha": "最终已推送提交的完整SHA", "report_paths": ["Docs/Reports/任务报告.md"], "screenshot_paths": []}
    return f"""[AUTO-RELAY-TASK:{task['run_id']}]
仓库：{config['repository']['url']}
分支：{config['repository']['branch']}
当前工作目录已经快进同步到 {synced_sha}。
请确认 origin、当前分支和上述版本，阅读仓库 AGENTS.md、任务文件 {task['task_path']} 及相关资料，然后完成任务 {task['task_id']}。
本轮动作：{task['action']}。请遵守任务书的范围和权限；不要改动传话工具本身。
遇到有未提交外部改动、分支冲突、无法完成的权限/环境要求或需要用户判断时明确停止，不覆盖已有工作。
按原项目流程完成实现和必要检查，提交并推送到已配置远端分支。不要 force push，不要宣称未运行检查已通过。
报告应注明代码版本、检查命令、结果、未运行项、截图路径和限制。视觉变更应提供真实截图；无法获取时在报告中说明。
最终完成后单独输出 JSON 交付块。commit_sha 必须是已包含全部报告和证据并成功推送的最终提交；路径相对于仓库、使用 /。
【交付】
{json.dumps(sample, ensure_ascii=False, indent=2)}
【交付结束】
"""
