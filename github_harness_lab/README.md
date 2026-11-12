# GitHub × DeepSeek Harness 本地试验区

## 配置你的 GitHub 仓库

双击上一级目录的 **`打开GitHub仓库设置.cmd`**（或 `github-harness.cmd`）。窗口中填写仓库地址、默认分支、允许派工的 GitHub 账号；本地代码目录和项目说明可选。点击 **保存配置**，写入本目录的 `config.local.json`。

点击 **生成通用部署包**，将配置快照、请求和通用部署包保存在本地 `.runtime/setup/`。其中 `generic/` 包含同一套执行器代码、该仓库的 `project-profile.json`、禁用的 GitHub 工作流及两份独立网页任务提示词。换仓库只需重新生成配置与部署包，不另写一套执行器。在 **接入与使用** 页点击 **保存并复制接入请求**，可将请求粘贴到当前 Codex 对话继续核对与审阅。**本机检查** 仅查看本地程序与文件是否存在，不登录、不读取凭据内容、不调用模型。

保存配置后显示“待接入部署”。本窗口不启动真实执行，也不会复用上次测试的授权。通用引擎目前提供 `bounded-document` 能力：两项文档任务各一次官方 DSH 零工具请求，第三项只发布。任意项目代码修改、UE 构建和游戏验收还没有接入这项能力。新仓库需部署匹配的工作流、独立受信安装、Runner 与两个网页对话中的任务，并核对新的启动授权；不能仅修改 `live_enabled` 来启动。下面历史真实测试使用的是旧固定控制器，不能算通用引擎在新仓库的实测证据。

命令行打开窗口：`..\.venv\Scripts\python.exe -m lab_harness.settings_ui`。通用 CLI 仍可用 `doctor --config config.local.json` 读取保存的配置；通用 `run --execute` 与 `publish --execute` 保持关闭。

## 通用执行器与项目配置

通用入口另见 `lab_harness/portable.py`：项目差异由 `project-profile.json` 配置，核心执行、授权、状态与发布逻辑不包含仓库专属任务分支。配置文件永远保持禁用；实际运行另需新激活记录、可信控制 Issue、匹配的部署 SHA 和代码/配置指纹。

以下命令仅在本地生成/检查文件，不联网、不启动模型或 Runner；输出路径必须尚不存在：

```powershell
..\.venv\Scripts\python.exe -m lab_harness.portable profile --from-config config.local.json --output .runtime/project-profile.json
..\.venv\Scripts\python.exe -m lab_harness.portable inspect --profile .runtime/project-profile.json
..\.venv\Scripts\python.exe -m lab_harness.portable bundle --profile .runtime/project-profile.json --output .runtime/project-bundle
```

部署包工作流是 `.yml.disabled`，仓库启用变量默认未设置，本机激活模板也为 `enabled=false`。生成作业不 checkout 项目，只读 GitHub；独立云发布作业从审阅过的工作流提交读取可信代码，校验精确产物与提交后创建两文件文档 PR，不合并。两条任务分支都从同一个部署基线独立建立，前一 PR 的 head 只用于证明先后关系。

文档请求在调用前持久化占用，重复或不确定结果不会自动补调用。同一仓库的所有 Runner 必须共用同一持久 `HARNESS_CONTROLLER_HOME` 及 `.runtime`，升级控制器时必须保留状态，不能为每个 Runner 分别复制状态目录。状态丢失时必须暂停激活并先只读对账，已有模型调用但尚未发布 PR 也可能已计费。当前锁不能保证跨目录或跨机器唯一。所有 `run_attempt>1` 均拒绝，云发布未知结果需要先核对，不能换虚拟机静默重跑。通用引擎离线检查与历史实测分别报告，文件生成成功不代表网页事件或实际模型流程已连通。

代码快照和指纹统一规范为 UTF-8（去除 BOM）与 LF 换行，Windows/Linux 或 Git 的 CRLF/LF 转换不会改变代码身份；实际字符或逻辑更改仍会改变指纹。

## 已完成的实测

**最新实测结果（2026-10-28）：简单文档任务已连续自动完成两轮，网页 GPT 已自动发布第三项。清理已完成。**

已验证：`GPT 发布 TASK_M0_1 → DSH 报告 → GPT 自动发布 TASK_M0_2 → DSH 报告 → GPT 自动发布 TASK_M0_3（只发布、不执行）`。对应 [Issue #9](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/9) → [报告 PR #10](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/10) → [Issue #11](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/11) → [报告 PR #12](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/12) → [Issue #13](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/13)。两份文档 PR 未合并，第三项 `dispatch=false`，没有执行。

成功复测使用 main `a908bf3957edb91bd842992cf83cab88b3d3b2ef`，北京时间 **16:24:05—16:28:34** 完成上述顺序，真实 DSH **2 次请求、359 tokens**。独立网页报告任务实际接收到两个 `github-actions[bot]` 的 PR `opened` 事件并发布后续任务。此前一次尝试完成一项后被共用对话中的启动任务打断，不计为成功；连同该尝试，本次共 **3 次请求、566 tokens**。完整证据见 [连续两轮结果](docs/SERIAL_TWO_ROUND_RESULT.md)。

所有网页测试任务已暂停，三个一次性 Runner 全部以 code 0 退出、服务端已无注册，Actions 创建/批准 PR 权限已恢复关闭，本地 serial 执行开关已关闭；没有常驻服务。本次仅证明受限的简单文档连续派工，不能据此声称长期无人值守或任意编码已验证。旧返工实验已撤销且为 0 次模型调用。以下为此前单次流程的历史记录。

已建立独立原型，原项目的桌面传话脚本和配置未改动。2026-10-28 用户授权后，已完成 **真实 Issue → 本地 Windows Runner → 官方 DeepSeek Harness → 测试 → PR**：见 [真实流程记录](docs/REAL_ISSUE_DEEPSEEK_PR.md) 和 [PR #4](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/4)。原型通用 CLI 仍默认不发送；本次采用独立、受限的单任务入口。

**此前单次算术流程记录：** 真实模型生成固定算术函数，本地 7 项、云端独立 10 项及 PR CI 的 8 项测试通过，GitHub Actions 自动创建 PR。机器人 PR 的 CI 当时曾需当前会话批准，网页定时验收当时尚未运行。该次一次性 Runner 已退出并注销，临时 PR 权限已恢复。通用 `run --execute` 与 `publish --execute` 继续 BLOCKED；长期运行、任意代码隔离、远程恢复和自动返工仍须单独完成。

## 立即在本地试用

此前准备的两次自动返工方案已停止使用，保留为历史代码；当前采用上方的简单连续派工方案。[Issue #5](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/5) 仅为实测控制记录。

在本目录运行，沿用父目录现有 Python 3.12 环境。核心代码只依赖标准库，不需安装包：

```powershell
..\.venv\Scripts\python.exe -m lab_harness doctor --config config.example.json
..\.venv\Scripts\python.exe -m lab_harness plan --config config.example.json --event examples/issue-event.json
..\.venv\Scripts\python.exe -m lab_harness demo --output .runtime/demo --scenario all
..\.venv\Scripts\python.exe scripts/verify_local.py
```

`doctor` 只检查路径和配置，不启动 Harness 桌面或 CLI；`plan` 不执行项目代码。`demo` 在新的本地临时仓库中用假 Harness 修改代码，外层真正运行测试、产生 Git commit、Evidence Manifest 和 outbox 待发布清单。所有模拟证据标记 `offline-demo`，PR 为 null、远程 CI 为 NOT_RUN。outbox 中 `sent` 为 false。

完整检查输出到 `.runtime/verification/report.json` 和 `unittest.log`，演示证据位置由报告给出。可选完整 JSON Schema / YAML 解析器在 `requirements-dev.txt`；当前没有安装这些包，报告会如实列为 NOT_RUN，不影响标准库测试。

## 文件与用途

| 路径 | 用途 |
| --- | --- |
| `lab_harness/worker.py`、`gitops.py` | 隔离本地 clone、真实测试、范围检查、控制进程提交、断点 journal、outbox；增量返工保留前一提交 |
| `lab_harness/state.py` | SQLite 事务、事件去重、单 Issue 租约、旧 owner fencing、最多三次返工、失败熔断、head 绑定验收与暂停 |
| `lab_harness/dsh.py`、`dsh_legacy.py`、`process.py` | 官方协议、Session/完成状态/退出码校验、日志限额和进程清理；通用执行入口仍关闭 |
| `lab_harness/bounded_dsh.py`、`live_smoke.py`、`smoke_policy.py`、`live_tools/` | 已真实验证的受限算术任务：官方 DSH 零工具/单请求、AST 白名单、本地测试与独立云端发布 |
| `lab_harness/policy.py`、`evidence.py` | 来源、路径、敏感文件与授权门禁；机器证据不依赖模型文字声明 |
| `lab_harness/github.py` | 官方 REST 基础客户端，默认禁止网络、每次请求检查授权、只读有界重试、未知写入结果禁止盲重发；未接入 CLI，只用假传输测试 |
| `lab_harness/controller.py` | 只读快照校验 Issue / 单一 PR / head / 条款摘要及读取期间变化；未核实标签操作者，不提供原子租约，不授权发布，未接 CLI |
| `workflows/*.yml` | 未激活的 GitHub Actions 调度计划和离线验证模板 |
| `live_fixture/` | 已上传私有测试仓库的最小 GitHub 联动样例：Issue 回执工作流、PR CI 和 10 项确定性测试 |
| `scripts/*.ps1` | Windows 检查、目录准备、固定版本 Runner / Harness 安装和注册；默认 Plan |
| `templates/task-issue.yml` | 带机器可读任务区的 Issue Form；测试只引用可信配置 ID |
| `schemas/evidence.schema.json` | 提交、测试、日志、CI、图形证据的统一 Schema |
| `prompts/project-manager.md` | ChatGPT 网页项目经理时间任务完整提示词 |
| `prompts/pr-reviewer.md` | ChatGPT 网页 PR 事件验收完整提示词 |
| `tests/` | 离线状态、安全、进程、Git、Worker 崩溃恢复和 CLI 防发送测试 |
| `config.example.json`、`examples/` | 全部默认关闭的配置与任务事件示例；授权示例故意无效 |

## 已验证的能力与边界

本地正常执行、重复事件跳过、增量返工、三轮返工上限、三次失败熔断、提交后中断恢复、旧 owner 拒绝更新、候选代码变更使旧测试证据失效均有测试。进程超时后清理子孙进程；模型最终 JSON 和真实测试退出码分别检查，超时/日志超限即使退出码为零也不能通过。Worker 禁止 Agent 提交、修改 `.git` 或隐藏暂存区内容。

这些是控制层检查，不能证明恶意代码无法读取主机或出站泄密。Windows Harness 的读取隔离不足；必须使用专用隔离主机/VM和网络策略。当前仅有进程级超时及测试前总时长检查，尚未实现覆盖全部 Git/Harness 阶段的 attempt 硬截止，也没有运行中的磁盘配额。CLI 超时不能代替供应商硬预算。

SQLite 是本地执行缓存；跨 Agent 权威状态应写入 GitHub。远程状态重建未接通，不能声称删除数据库后仍能自动恢复全部线上状态。

截至 2026-10-28，本机环境实测：当前目录未初始化 Git；已有 venv Python 3.12.14；RTX 4090 / 驱动 610.88；PATH 无 `dsh` 与 `nvcc`。后续按实际安装路径找到官方 DSH CLI，版本为 0.2.1-rc.3，版本和帮助命令可用，见 [本机 DSH 预检](docs/LIVE_DSH_PREFLIGHT.md)。显卡只用于需要 GPU 的任务。

## 网页任务与上线准备

官方已公开符合资格账户的 GitHub PR 事件触发能力，时间与事件必须分成两个任务；不能假定 Issue 事件直接触发 ChatGPT。见 [OpenAI Scheduled tasks](https://learn.chatgpt.com/docs/automations)。当前 Codex 会话已实测 GitHub 写入，但这不证明 ChatGPT 网页 Scheduled Tasks 的后台写入和免逐次审批能力，仍需独立实测。

早期通用方案的两个完整提示词在 `prompts/`，当时仅保存文件，没有创建 Scheduled Task；后续已完成本页顶部所述两个真实网页任务配置。普通 `workflows/` 目录保留早期计划模板；当前连续派工使用 `live_tools/serial-smoke.yml`，已部署为测试仓库的 `.github/workflows/serial-smoke.yml`。不要把早期模板的 `HARNESS_LAB_ENABLED` 开关与本次受限控制记录混为一谈。

部署、权限、Secrets、首次网页授权、启动暂停和真实 A–F 验收步骤分别见：

- [能力核查与官方来源](docs/FEASIBILITY.md)
- [分阶段实施状态与剩余开发](docs/IMPLEMENTATION_STATUS.md)
- [Windows 部署与维护](docs/DEPLOYMENT.md)
- [DeepSeek 官方接口与配置](docs/DEEPSEEK.md)
- [安全配置](docs/SECURITY.md)
- [故障恢复](docs/RECOVERY.md)
- [完整演示与真实验收步骤](docs/DEMO.md)
- [首轮离线验证记录：119 项测试通过](docs/VERIFICATION.md)
- [真实 GitHub 联动、证据与复现方法](docs/LIVE_INTEGRATION.md)
- [本机官方 DSH 版本与兼容性预检](docs/LIVE_DSH_PREFLIGHT.md)
- [真实 Issue → DeepSeek → PR 结果与复现](docs/REAL_ISSUE_DEEPSEEK_PR.md)

本地控制命令如下。`resume` 只解除暂停，不授权发送；演示数据库位于每次 demo 输出的 `root/runtime/state.sqlite3`，查看演示状态时请替换路径：

```powershell
..\.venv\Scripts\python.exe -m lab_harness status --state .runtime/state.sqlite3
..\.venv\Scripts\python.exe -m lab_harness pause --state .runtime/state.sqlite3
..\.venv\Scripts\python.exe -m lab_harness resume --state .runtime/state.sqlite3
```
