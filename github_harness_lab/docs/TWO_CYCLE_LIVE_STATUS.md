# 网页任务连续派工实测状态

**最新状态（2026-10-28，Asia/Shanghai）：真实连续两轮已通过，第三项已由网页 GPT 自动发布，清理完成。** 用户要求的“两轮”是两个简单任务依次完成，不是同一 PR 返工两次。详细证据见 [连续两轮结果](SERIAL_TWO_ROUND_RESULT.md)。

成功顺序为：`TASK_M0_1 Issue #9 → DSH 报告 PR #10 → TASK_M0_2 Issue #11 → DSH 报告 PR #12 → TASK_M0_3 Issue #13`。北京时间 16:24:05 发布第一项，16:26:05 自动发布第二项，16:28:34 自动发布第三项。两份文档已真实完成；第三项 `dispatch=false`，未执行。后两次派工有独立网页报告任务的真实机器人 PR `opened` 事件及运行记录支持。

- 控制记录：[Issue #5](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/5)，实验标识 `GPTDSH_SERIAL_M0`。
- 成功复测固定 main：`a908bf3957edb91bd842992cf83cab88b3d3b2ef`；nonce：`d11729d0-6c10-4199-9066-b78cf1b4f93e`。
- 网页启动任务：`6ae19d13b64c8190a3808ad486375c7c`，现已暂停。
- 成功复测独立网页报告任务：`6ae1b067d5b48191965a3d6af74eaa2d`，现已暂停；替代共用对话中的旧报告任务 `6ae1a01927648191a716fc3f82e35179`，旧任务也保持暂停。
- 成功复测 **2 次 DSH 请求、359 tokens**。此前 Issue #7 / PR #8 尝试完成一项后被共用对话中的启动任务打断，已归档，不计为连续成功；该次 207 tokens。合计 **3 次请求、566 tokens**。
- 本次在北京时间 **16:58:46** 截止前完成；两份文档 PR 未合并，第三项未执行。
- 已通过 **23 项针对性离线测试**。测试并未调用真实模型，不能替代真实网页运行、GitHub 任务/报告及 DSH Session 证据。
- 旧 `DSH-REWORK-002` 启动记录已撤销，旧返工实验 **0 次模型调用**。旧代码保留，但不再作为本次任务定义。
- 清理已验证：所有网页测试任务暂停；三个 ephemeral Runner 全部 code 0 退出，服务端显示 `There are no runners configured`；Actions 创建/批准 PR 权限恢复 OFF；本地 serial `enabled=false`；无常驻服务。

本次已证明简单文档任务的两次“报告 → 下一任务”自动转换；长期无人值守、任意代码任务、自动合并及其他故障恢复仍不在本次证明范围内。后续历史区只用于保留先前准备过程。

## 历史：已撤销的两轮返工准备方案

以下保留的是改为连续派工之前的准备快照与旧方案，**不再作为当前操作步骤或当前状态**。准备 PR #6 后续已按授权合入，短期 Runner 与权限已进入本次受限实测窗口；以本页最上方新方案为准。

2026-10-28，Asia/Shanghai。专用任务：[Issue #5](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/5)，`DSH-REWORK-002`。

**该历史快照当时为 PREPARING，真实网页两轮为 NOT_VERIFIED。** 当时已实现控制器、独立发布/测试、等待验收、协议、两份网页配置和三个一次性 Runner 的准备/注册脚本。两个真正的网页任务当时均已创建并暂停；三个官方 Runner 程序当时已准备但未注册/启动，尚未发布 arm 或调用 DeepSeek。不能把下面测试结果作为线上两轮成功证明。

准备代码为 [PR #6](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/6)，不是模型生成的候选PR。初版提交5cd583f6的真实GitHub CI [40647781060](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40647781060)通过99项（含离线控制平面测试）。

- 网页 manager：`6ae19d13b64c8190a3808ad486375c7c`，PAUSED，Hourly，Asia/Shanghai。
- 网页 reviewer：`6ae1a01927648191a716fc3f82e35179`，PAUSED，GitHub事件，仓库精确匹配、标题匹配DSH-REWORK-002、PR/review评论及commit updates，无时间计划。
- 用户先后明确允许首次填写官方表单及发送一次配置指令。后续循环不模拟输入、不手动Run now。
- 一次性配置对话：`https://chatgpt.com/c/6ae19fda-4734-83ec-b863-374806f468e2`。触发器和PAUSED已另在官方编辑界面核实。
- 仍需真实验证机器人candidate通知能否触发网页验收；配置对话的自然语言答复不是运行证明。

## 已核查

- 当前 ChatGPT 网页确有 Scheduled 与官方新建任务表单。当前会话没有普通网页 Scheduled Task 创建工具；Codex automation 工具仅对应本地任务，不用于替代。
- GitHub 插件的 app-specific permission 为 **Allow all actions**，本次只读核查，没有更改。这并不保证网页每一次后台执行均成功，仍须实测。
- [官方 Scheduled tasks](https://learn.chatgpt.com/docs/automations) 支持定时和 GitHub PR 事件，但必须分两个任务。依据 [Work cloud security](https://learn.chatgpt.com/docs/enterprise/chatgpt-work-cloud-security)，后台写操作是否免确认还取决于连接与权限策略。
- 用户原任务卡禁止模拟 ChatGPT 网页输入，已取得上述仅首次配置的明确例外；不把此例外用于无人值守循环。

## 实验定义

按较强口径验证：候选 H0 FAIL → 自动返工 H1 FAIL → 自动返工 H2 PASS，共三个不同 head、三个真实 DSH Session、两份真实失败验收与一份通过验收。

为确保覆盖两个返工回路，原 Issue 公开说明两次**控制器故障注入**：每次真实 DSH 先修复算术并实际通过10项测试；revision 0 将 multiply 替为 left+right，revision 1 替为 left*right+1，revision 2 不注入。保留模型原始源码/摘要与实际发布源码/摘要，真实执行失败断言。验收者看实际源码和测试，不能按轮次固定判 FAIL。

工作流为9个串行阶段：generate-0 → publish-0 → wait-0 → generate-1 → publish-1 → wait-1 → generate-2 → publish-2 → wait-2。三次生成各占一个 ephemeral Runner；不安装服务。发布器只写专用分支和同一个草稿PR，重新下载精确head的源码执行可信测试。发布作业结束后，等待作业只读轮询真实网页验收评论，确保验收时能读取已完成的CI日志。

检查上下文 `dsh-cycle/acceptance-harness` 与每轮head绑定。旧 pull_request CI 可能仍 action_required；依照 [GitHub 官方触发规则](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)，不依赖机器人事件递归，也不把待审批的PR CI声称为通过。

## 当前验证

- 完整本地 unittest 在新增最后两项校验前通过 **264项**。
- 随后的控制器校验加强后，`test_live_cycle.py` **17项**通过；拆分等待作业后 `test_cycle_publisher.py` **21项**通过。
- 这些测试中 GitHub/DSH 是假传输/假模型，算术测试是真实子进程。故障候选的测试日志预期显示 FAIL，外层断言验证这一失败行为；外层测试集通过。
- `two-cycle.yml` 已用 PyYAML 解析，9个job及依赖核对通过；准备/注册脚本通过 PowerShell 语法解析。
- 工作流 `CYCLE_AUTOMATION_ID` 已填真实reviewer ID；`.private/live-cycle.json` 尚未创建，未发布 arm，不会启动实验。

## 继续所需配置

1. 已用 `prompts/two-cycle-manager.md` 与 `prompts/two-cycle-reviewer.md` 创建网页任务并暂停；启动前设定manager未来的每小时执行分钟，等待真正定时唤醒，不点击Run now。
2. 填入真实 reviewer automation ID，部署并固定workflow SHA、Issue正文SHA256与初始源码摘要。生成本地只包含这些绑定及DSH路径的配置，不上传凭据。
3. 本次单独确认短期注册三个一次性Runner及临时允许Actions创建PR。此前只有一次任务的授权已用完并恢复，不能沿用为常驻或扩大范围。最迟到本次实验截止时停止监听并删除未用注册，恢复原PR权限。
4. 两项网页任务与三个Runner就绪后，发布90分钟有效arm（最多3模型请求、每次512输出token/120秒，每次等验收最多20分钟），启用网页任务。真正定时manager写start收据和初始标签；本会话不代写。
5. 收集每次网页实际运行、GitHub评论ID、三个head、每阶段Actions/DSH/测试证据。中途有人工派工、Run now、手动重跑或手写验收，该段不能算连续自动循环。缺网页来源证据仍为NOT_VERIFIED。

`replay_journal(..., mode='operational')` 可用已认证owner评论驱动返工，但不会声称证明了网页来源。最终live核验还需要从官方运行记录独立核对，不能用评论自报来源或测试回调代替。
