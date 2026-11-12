# Windows 部署与维护

通用原型默认关闭真实执行和发送。本文记录通用部署要求；用户随后授权的受限真实测试已完成 Runner 注册、模型调用和 PR 创建，详情见 [真实流程记录](REAL_ISSUE_DEEPSEEK_PR.md)。本轮专用脚本是 `Prepare-LiveRunner.ps1` / `Connect-LiveRunner.ps1`；一次性 Runner 已退出，没有常驻服务。

## 先验证本地原型

在 `github_harness_lab` 目录运行，使用父目录已存在的 Python 环境：

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
..\.venv\Scripts\python.exe -m lab_harness plan --config config.example.json --event examples/issue-event.json
..\.venv\Scripts\python.exe -m lab_harness demo --output .runtime/demo --scenario all
..\.venv\Scripts\python.exe -m lab_harness doctor --config config.example.json
```

`plan` 不调用模型或 GitHub，`demo` 仅使用本地假 Harness 与临时 Git 仓库，`doctor` 不提交提示词。示例中的 `OWNER/REPOSITORY` 是占位符。新机器可用受信的 Python 3.12 安装创建独立 venv；核心模块仅使用标准库，勿复用现有桌面自动发送入口。

## 一次性授权与配置清单

1. 选择私有测试仓库，检查源码、现有测试与 CI。将工作流模板复制到**目标仓库根目录** `.github/workflows/`，Issue 模板复制到 `.github/ISSUE_TEMPLATE/`；本目录内的模板不会被 Actions 自动激活。先保持上线变量关闭。
2. 配置默认分支保护：只允许 PR；所需 CI 对当前 head 成功；禁止 Worker 绕过保护和直接推默认分支；禁用强推/删除。维护者手工创建 `agent:ready`、`agent:running`、`agent:review`、`agent:rework`、`agent:accepted`、`agent:blocked`、`agent:failed` 标签。
3. 配置专用 Runner / 执行 VM、非管理员账户和目录 ACL。不要把 OneDrive 文档、个人用户目录、SSH、浏览器或既有 Git 凭据挂入执行环境。只有需要 GPU 的任务才调度 GPU 标签；CUDA toolkit 需按任务要求另行核查。
4. 从仓库 Settings → Actions → Runners 获取 Windows 官方安装信息、固定版本和校验摘要，检查部署脚本的 Plan 输出。Windows 服务安装需要管理员进行一次性操作，但运行身份必须为专用低权限账户，不能沿用管理员或 SYSTEM。
5. 从官方来源安装并固定 DeepSeek Harness，记录安装版本、`--help` 和 headless 帮助；按 [DEEPSEEK.md](DEEPSEEK.md) 实测权限和 Session。不要仅因为桌面 Harness 能打开就认为 CLI 可用。
6. 把 API 密钥放入专用账户可访问的安全凭据配置，给执行进程最小环境。GitHub 发布凭据只交给可信发布进程；不要把它留在 Agent 环境、Git remote URL、持久 credential helper 或 checkout 的 HTTP extraheader。为供应商设置独立硬预算；时长上限不能保证金额上限。
7. 复制 `config.example.json` 为本地配置，填写准确仓库、可信作者、允许/禁止路径、可信测试 ID 与 argv、超时、默认分支、成本和隔离证明。真实门禁要求 `live_enabled`、未过期且仓库一致的授权文件、`provider_hard_budget_confirmed`、`isolated_environment_verified`；这些声明需要真实证据，不能为了启动而随意改成 true。
8. 用户同意后，在 ChatGPT **网页**连接目标仓库，手动运行两个提示词，分别验证读取、Issue 创建、标签、评论和 Review 的权限。若后台写操作逐次需确认，保留阻塞并报告，不能绕开。

Runner 由主机向 GitHub 建立出站连接，不需本机公开 HTTP 服务。网络需允许官方列出的域名与 HTTPS 443，见 [Self-hosted runners reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)。Windows Docker container actions / service containers 需额外 Linux 执行节点，不能直接假定原生 Windows Runner 支持。

## 凭据与权限

| 配置 | 最小用途 | 存放位置 |
| --- | --- | --- |
| DeepSeek API Key | Agent 访问已批准供应商模型 | 专用执行环境安全凭据；不写入仓库 |
| 发布 GitHub App / scoped token | 仅目标仓库 contents、issues、pull requests 所需写；actions/checks 只读 | 可信发布边界，按工作流配置 Secrets 或外部安全存储 |
| Runner registration token | 一次性注册 | 注册时临时输入，不保存日志/配置；有效期有限 |
| ChatGPT GitHub connection | 项目经理与验收所需读写 | ChatGPT 网页插件授权和工作区政策 |
| 仓库上线变量 | 禁止误触发真实执行 | 目标仓库 Variables，默认关闭 |
| 本地授权文件 | 明确仓库、范围、截止时间的本次上线授权 | 仅可信控制进程可改，执行账户不可写 |

不要给 Worker 管理员、仓库 administration、Secrets 管理、保护规则绕过或组织全局写权限。GitHub App 令牌与普通 `GITHUB_TOKEN` 的后续 CI 行为必须按 [FEASIBILITY.md](FEASIBILITY.md) 在仓库实测。注册和 Windows 服务要求见 [Adding self-hosted runners](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)。

## 启动、暂停、恢复、关闭

```powershell
..\.venv\Scripts\python.exe -m lab_harness status --state .runtime/state.sqlite3
..\.venv\Scripts\python.exe -m lab_harness pause --state .runtime/state.sqlite3
..\.venv\Scripts\python.exe -m lab_harness resume --state .runtime/state.sqlite3
```

暂停本地领取不保证立即终止已运行的进程。紧急停止先关闭仓库上线开关并暂停两个网页任务，再取消 Actions 运行并停止专用 Runner 服务；核实进程树已清理。恢复时先对账 PR/head、持久化检查点与 CI，再解除暂停。不要删除状态数据库或换 Task ID 来重试。

本期 `run --config <本地配置> --event <事件文件>` 默认只计划；即使添加 `--execute` 并满足授权文件，仍因可信远程控制桥未接通而返回 BLOCKED。`publish --config <本地配置> --outbox <待发送清单>` 只展示待发送计划，`--execute` 也明确阻塞。因此仅修改开关不能启动真实发送。

要进入真实部署，还需单独完成认证远程快照读取、跨 Runner 的 GitHub journal 重建、受控执行环境对接和独立发布 job，并再次接受安全评审和用户授权。现有工作流仅调度离线计划/验证。未来真实 outbox 必须复核仓库、分支、提交 SHA、证据摘要及待发送内容。

## 部署脚本预览

以下均为本地 Plan / 只读检查，不安装、不注册：

```powershell
.\scripts\Inspect-Windows.ps1
.\scripts\Initialize-Windows.ps1 -Root C:\dsh-lab
.\scripts\Install-Runner.ps1 -Version <官方精确版本> -Sha256 <官方SHA256> -Root C:\dsh-lab\runner
.\scripts\Register-Runner.ps1 -Repository OWNER/REPOSITORY -Root C:\dsh-lab\runner
.\scripts\Install-Harness.ps1 -Version <官方精确npm版本> -Root C:\dsh-lab\tools
```

尖括号必须替换后再执行。脚本的修改模式还需 `-Execute -Repository OWNER/REPOSITORY -AuthorizationFile <文件>` 和包含 `deploy` scope 的授权，不能本轮启用。注册令牌仅临时通过 `ACTIONS_RUNNER_INPUT_TOKEN` 进程环境传递。Harness 安装使用 `npm --ignore-scripts`，安装后必须人工验证 CLI 能运行；脚本不宣称已完成真实部署。脚本不创建管理员运行的常驻 Agent，也不自动配置 Windows 服务。

工作流假设目标仓库根目录含整个 `github_harness_lab/` 代码目录。注册脚本与调度模板使用同一个 `dsh-lab` Runner 标签；该标签不证明隔离已经合格。`HARNESS_LAB_CONFIG` 应指向受信控制账户持有的配置绝对路径，默认不设置，`HARNESS_LAB_ENABLED` 默认关闭。若选择将试验区作为仓库根目录，须先调整工作流中的路径再评审。

## 日常维护

- Runner 自动更新、Windows 补丁、Git/Python/DSH 固定版本与供应商模型变化均需定期复核；升级先离线回归，再在专用仓库做一次真实闭环。
- 将状态、日志与证据放在代码工作区之外的受限目录。日志限额/保留期应按磁盘空间设置，先确认无运行和已上传可恢复证据，再人工清理过期目录；不要递归清空工作根或会话目录。
- 每次运行保留独立目录、Task ID、attempt、session、base/result SHA 与 Actions URL；只上传经检查的测试证据，原始 Harness 流留本地受限存储。
- 重启恢复需由批准后的 Runner Windows 服务和可信状态重建完成，不依赖个人浏览器持续打开。云端网页任务可关闭浏览器后运行，但本地 Windows 主机和 Runner 仍须在线。
