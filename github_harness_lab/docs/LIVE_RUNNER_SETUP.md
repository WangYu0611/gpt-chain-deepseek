# 本机 Runner 接通预检与一次性配置

检查时间：2026-10-28。目标固定为私有仓库 `WangYu0611/chatgpt-deepseek-harness-test-20261028`。本预检没有注册 Runner、安装服务、修改权限、读取凭据或执行模型。

## 本机实测

| 项目 | 结果 |
| --- | --- |
| Windows | Windows 11 专业版，10.0.26300，64 位 |
| 当前身份 | `BH4090\wy331`；非提权，进程 token 中无 Administrators 组 SID |
| Git / Python | Git 2.55.0.windows.5；父项目 venv Python 3.12.14 |
| 已装 Runner | 未发现 `actions.runner.*` 服务；`C:\actions-runner`、`C:\dsh-lab\runner`、`%LOCALAPPDATA%\dsh-lab\runner` 均不存在。这是有限目标检查，不代表搜索了整台电脑 |
| WSL | `wsl.exe --list --quiet` 返回 1，明确提示 Windows Subsystem for Linux 未安装 |
| Windows Sandbox | 系统目录不存在 WindowsSandbox.exe |
| 其他隔离入口 | Docker、VirtualBox、VMware CLI 不在 PATH；未发现对应已检查的服务 |
| 虚拟化 | HypervisorPresent=true，仅证明检测到 hypervisor，不证明存在可用、已隔离的执行 VM |

当前普通用户可以运行标准 Runner 进程，不需要管理员。需要服务时才涉及 Windows 管理员的一次性服务安装，本次单次验证不安装服务。个人用户目录能访问个人文件，非管理员身份也不等于隔离；任意 Agent 工具或模型生成代码不得据此直接认定安全。

## 已核查的官方固定版本

- 版本：**2.338.2**，2026-10-26 发布；查询 GitHub 官方 releases/latest API 与 release 页一致。
- 包：`actions-runner-win-x64-2.338.2.zip`。
- SHA-256：`a1f17aa987fc8158bc7521d284912267a0d01b7c2c4684b33c86783b5e8221cc`。
- [官方发布与校验和](https://github.com/actions/runner/releases/tag/v2.338.2)。GitHub 渐进发布可能使仓库推荐版本有差异，以此仓库 Settings 的下载提示复核。

准备脚本固定上述官方包及摘要，拒绝非空目标目录，校验后才解压，并检查二进制版本。默认位置在 `%LOCALAPPDATA%\HarnessGithubSmoke\runner`，避免把 Runner 凭据同步到 OneDrive；尚未对该位置写入文件。

```powershell
# 从 github_harness_lab 目录运行；默认只展示计划。
.\scripts\Prepare-LiveRunner.ps1
.\scripts\Connect-LiveRunner.ps1

# 下载并验证官方程序，不注册、不启动。
.\scripts\Prepare-LiveRunner.ps1 -Execute
```

## 需要用户完成的一次性步骤

1. 在此测试仓库 Settings → Actions → Runners → New self-hosted runner，选择 Windows / x64。获取此仓库的短时 registration token，有效期一小时。**不要把 token 发到聊天、提交到仓库或粘进普通命令行。** [官方注册步骤](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
2. 确认即将调度的工作流只处理本次固定测试，使用 `dsh-smoke-20261028` 专用标签。注册使用 `--no-default-labels`，避免通用 `self-hosted` / `Windows` / `X64` job 意外命中。只有任务控制代码、执行边界和模型配置准备完成后才启动 Runner。
3. 在本机普通 PowerShell 中执行下面的命令。脚本隐藏输入 token，使用官方环境变量方式传入注册程序，不向参数或日志输出 token；GitHub 官方 Runner 自己会生成本地注册凭据。脚本只固定注册到这个测试仓库。

```powershell
# 注册后暂不监听任务，便于先完成配置。
.\scripts\Connect-LiveRunner.ps1 -Execute

# 待工作流审核与模型配置完成，再启动。
& "$env:LOCALAPPDATA\HarnessGithubSmoke\runner\run.cmd"
```

如果所有条件已准备好，可在首次注册时用 `-Execute -Start` 一次执行。脚本拒绝覆盖现有注册、不安装服务、不使用管理员账户。`--ephemeral` 使 GitHub 只分配一个 job，完成后自动注销；它不会自动隔离或清理个人主机文件。一次测试之后不要直接把此目录作为长期无人值守 Runner。`--disableupdate` 固定本次检查版本，后续部署前须重新核查版本有效性。[官方一次性 Runner 行为](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)

## 路径与剩余条件

- 若执行通用 Agent 工具或任意模型代码：需专用标准账户或独立 VM，并隔离个人文件/凭据、限制网络和权限。当前没有已验证的 VM，本次不能把布尔配置改为 true 代替隔离实测。
- 本轮计划采用禁用全部模型工具、固定输入、输出作为数据、控制器使用 AST 白名单限定结构的单次生成：不执行任意模型代码，模型进程不继承 GitHub 发布 token。当前非管理员账户可用于这个明确受限的 smoke，但必须独立验证模型没有 shell/文件/网络工具。不能用这一结果证明通用 Worker 隔离。
- GitHub `GITHUB_TOKEN` 产生的 push/PR 通常不触发后续工作流；发布阶段需独立受信凭据或当前授权连接器，或显式调度 CI。测试通过记录必须读取真实运行日志。使用 `GITHUB_TOKEN` 创建 PR 还取决于仓库 Actions 设置。
- 运行方式不要求公网入站端口；Runner 向 GitHub 建立出站连接。完整可达性以实际注册和“Listening for Jobs”为准，本次未宣称完成注册。[官方网络与系统要求](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)

停止等待中的 Runner：在运行终端按 Ctrl+C；若已有 job，先在 GitHub Actions 取消该 job，再停止 Runner。长期服务安装、开机启动、账户创建和网络 ACL 不属于这两个单次脚本。

令牌环境变量与 Windows `ephemeral` / `disableupdate` 参数已核查 [固定版本官方 CommandSettings 源码](https://github.com/actions/runner/blob/v2.338.2/src/Runner.Listener/CommandSettings.cs)。脚本测试仅包括 PowerShell AST 与 Plan，不是已注册成功的证据。
