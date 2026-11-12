# P0 可行性核查

核查日期：2026-10-28（Asia/Shanghai）。状态：**本地原型，可离线验证；真实端到端闭环未验证**。用户尚未授权真实发送，本轮没有创建 Issue / PR / Scheduled Task，没有注册或启动 Runner，没有调用 DeepSeek 模型。

## 当前项目与运行环境

- 项目根路径 `C:\Users\wy331\OneDrive\文档\ChatGPT\gpt_chain_deepseek` 当前不是 Git 仓库，无法从 `.git` 判定远程、默认分支或保护规则。
- 原有项目是 Python 桌面 GPT ↔ DSH 传话工具，含 `relay/`、`windowrelay/` 和 `unittest` 测试；部分现有功能使用网页/桌面输入。新目录 `github_harness_lab/` 独立实现，不复用该通信路线。
- `.venv\Scripts\python.exe` 为 Python 3.12.14；PATH 中 `python` 是 Windows Store 占位入口。Git 可用，`dsh` / `gh` 未在 PATH 中发现。DSH 桌面程序存在不等于 headless CLI 已安装并验证。
- 只读检测：NVIDIA GeForce RTX 4090，驱动 610.88，显存 24564 MiB；C 盘剩余约 1.8 TB。GPU 存在不证明 CUDA toolkit 或图形测试可用。
- 未读取真实配置里的私密字段，未验证任何远程仓库的写权限、CI、Secrets、分支保护或组织策略。

## 已公开支持与待验证边界

| 环节 | 官方公开能力 | 本项目状态 / 验证门槛 |
| --- | --- | --- |
| ChatGPT 网页 Scheduled Task | 可用连接工具，eligible 账户支持 GitHub PR 活动事件；事件计划与时间计划需分开 | 提供两个完整提示词；账户套餐、工作区开关及实际触发尚未核实 |
| Issue → ChatGPT | 官方公开列表没有保证 GitHub Issue 事件触发 | 项目经理按时间检查 GitHub；不把 Issue 当成网页任务事件 |
| ChatGPT 写 GitHub | 实际可写范围取决于连接器与账户授权 | Issue 创建、标签、PR 评论及 Review 都需首次真实验证；若每次需人工确认，该环节不能无人值守 |
| PR 新提交 → 重新验收 | PR commit updates 是公开事件类型 | 提示词绑定 head SHA；事件批合并和延迟不能当作严格实时队列 |
| DeepSeek Harness | 仅采用官方 headless/session 协议 | 详见 [DEEPSEEK.md](DEEPSEEK.md)；本机真实 CLI 帮助、权限、Session、预算尚未端到端验证 |
| Actions → Windows | 自托管 Runner 支持 Windows，主机主动连接服务 | 提供关闭状态的工作流与部署脚本；没有安装、注册或启动 |
| 自动提交与 PR CI | 令牌类型影响后续事件和审批 | 使用最小权限 GitHub App / 受限凭据；必须验证实际 CI 递归触发策略 |
| 无人值守成本上限 | 本地计数与超时可限制轮次和时长 | 不能代替供应商硬额度；未确认硬预算时阻止真实调用 |

OpenAI 的公开支持来自 [Scheduled tasks 官方文档](https://learn.chatgpt.com/docs/automations)。它不替本账户保证可用性，也不证明所有 GitHub 写操作可在后台免逐次确认。

GitHub 当前规则中，`GITHUB_TOKEN` 引发的大部分事件不再递归触发工作流；它创建/更新 PR 的 opened、synchronize、reopened 会产生需要人工批准的 CI 运行。GitHub App installation token 或 PAT 的行为不同。必须在目标仓库实测，不能把“PR 已创建”当成“CI 自动运行”。参见 [Triggering a workflow](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)。

## 实施阶段与验收口径

| 阶段 | 本地交付 | 真实部署门槛 |
| --- | --- | --- |
| P0 | 仓库/环境检查、官方能力说明 | 指定目标仓库，确认写权限与计划能力 |
| P1 | 官方 DSH 适配、Windows 检查/部署脚本 | 隔离环境、低权限账户、固定版本、硬预算 |
| P2 | Issue 解析、离线受控执行与 PR outbox 计划 | 可信远程控制桥、独立发布 job、用户同意发送 |
| P3 | Commit 绑定的 Manifest、外层测试退出码 | 真实日志/产物可访问，可信 CI 验证 |
| P4 | 网页项目经理和 PR 验收提示词 | 在网页首次配置并实测两种触发 |
| P5 | 幂等、持久化、租约与返工熔断 | 故障恢复对账和远程状态重建演练 |
| P6 | 离线演示、自动化测试与手工验收步骤 | 真实 A–F 场景证据，尤其权限隔离实测 |

SQLite 是本机崩溃恢复缓存，跨 Agent 可见的权威记录仍是 GitHub Issue、PR 和 Actions。任何仅存在本机的检查点在发送前都不是另一 Agent 可据以行动的状态。离线模拟仅验证本地逻辑，不能替代真实 GitHub、DeepSeek 或网页任务验收。

## 尚未消除的阻塞

1. 未指定并验证目标 GitHub 仓库和可信作者。
2. 未批准真实发送；上线门禁仍关闭。
3. 未部署专用隔离执行环境。相同 Windows 用户下的路径检查、环境变量过滤和 worktree 不构成安全沙箱。
4. 未验证 DeepSeek 真实 CLI 版本、权限强制执行、API 额度硬上限和 Session 恢复。
5. 未验证 ChatGPT 网页两个任务在本账户的运行和写操作审批行为。
6. 未取得真实 Issue → Harness → PR → 当前 SHA CI → ChatGPT 验收的可核查证据。
7. 本期 `run --execute` 和 `publish --execute` 均保持 BLOCKED；尚需实现认证远程快照、跨 Runner GitHub journal 重建和独立可信发布控制桥。授权开关本身不会将离线原型变成线上闭环。
