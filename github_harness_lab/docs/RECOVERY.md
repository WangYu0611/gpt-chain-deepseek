# 状态与故障恢复

状态流：`READY → RUNNING → REVIEW → ACCEPTED`；验收需返工时为 `REVIEW → REWORK → RUNNING`，每个 Issue 最多三次自动返工。失败可记录 FAILED；权限、缺证据、预算、上限或无法判定的一致性问题进入 BLOCKED。暂停是控制开关，不篡改任务身份。

本地 SQLite 以事务维护事件幂等、租约、owner fencing、计数与检查点，用于同机崩溃恢复。GitHub 是跨 Agent 权威来源；未来线上控制桥需把可重建状态写入 Issue / PR / Actions journal。**本期没有完成跨 Runner 自动远程重建，不能删除数据库后假定会从 GitHub 自动恢复。**

## 通用恢复顺序

1. 暂停领取并关闭线上开关；真实发送当前仍禁用。保存数据库、运行目录与 outbox 的受限副本，不编辑数字来绕过上限。
2. 查询 Issue、当前任务分支、PR head、Actions 运行和最后一次可信检查点；区分未执行、执行完成未记录、已推送但创建 PR 响应丢失等状态。
3. 确认旧进程与子进程均退出。只有租约过期且旧 owner 被 fencing 后，新执行实例才可恢复同一 attempt；不能凭 Runner 显示离线就认为原 Agent 已停止。
4. 在**原 Task ID 和原任务分支**继续。远程写入超时先查询实际结果，再决定补齐，避免第二个 PR / 重复评论。
5. 恢复前重新验证 base/head、证据对应 SHA、权限和预算；信息不完整时 BLOCKED。按受控门禁重新放行，观察第一轮。

## 常见故障

| 故障 | 处理 |
| --- | --- |
| 同事件重复投递 | 重用稳定 event identity；返回 SKIPPED，另查持久状态和证据，不启动第二实例 |
| 同一 Issue 不同事件同时到来 | 仓库/Issue 调度串行，加持久化 lease；检查当下状态，不丢失待处理返工 |
| Runner 离线 | 查看 GitHub 队列、主机网络、服务和磁盘；勿重复贴 ready 或启动并行本地 Worker |
| Windows 重启 / Worker 崩溃 | 先终止遗留进程，等待租约并 fencing，再恢复检查点；证据未知时重跑外层可信检查 |
| Harness 崩溃 / final 缺失 | 不进入 REVIEW；保留受限 stdout，当前须人工从流中恢复 session，未实现失败 Session 自动续接；即使自然语言说成功也不算通过 |
| 测试失败 / CI 失败 | 保留真实退出码及对应 head；具体代码问题可进入有上限的返工，网络/权限问题 BLOCKED |
| GitHub API / 网络超时 | 只读请求有限重试；不确定写入先按任务标识和分支查询，不能盲目 POST |
| 分支冲突 / 主分支更新 | 暂停并核对新的 base；不可强推主分支，冲突处理后重新测试和验收 |
| PR 创建响应丢失 | 按 head 分支查询已有打开 PR；确认没有才重试，不换分支规避 |
| 新 commit 到达验收期间 | 重新取 head；原报告只关联旧 SHA，不能接受新提交 |
| 网页验收事件重复 / 自己评论触发 | 按 PR/head/criteria revision 去重，缺新证据不重复发报告 |
| 自动返工达到三次 | BLOCKED；等待人工改变任务范围或修复根因，不清零计数自动继续 |
| 本地状态丢失 | 保留现有分支和 PR，人工用 GitHub 权威记录对账；本期不提供未经验证的一键远程重建 |

## 本地暂停与恢复

```powershell
..\.venv\Scripts\python.exe -m lab_harness status --state .runtime/state.sqlite3
..\.venv\Scripts\python.exe -m lab_harness pause --state .runtime/state.sqlite3
..\.venv\Scripts\python.exe -m lab_harness resume --state .runtime/state.sqlite3
```

`resume` 不是授权发送，也不抹除 FAILED/BLOCKED 或已有返工次数。本期真实控制桥保持关闭，只有离线演示可继续。

本地清理前检查绝对路径是否处于专用运行根目录内，确认目录没有有效任务、会话或唯一证据，先备份再人工清理。不要用递归删除整个工作根来修复错误。
