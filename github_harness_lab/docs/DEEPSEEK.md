# DeepSeek Harness 适配记录（2026-10-28）

本次仅实现和离线测试适配器，没有向 DeepSeek 发送任务，没有安装包、注册或启动 Runner。当前桌面安装存在，但 PATH 中没有 `dsh`；不能把 Electron 主程序当作 CLI 执行。

## 已核查的公开接口

- 官方 CLI 帮助支持 `dsh --profile headless --permission-mode workspace-write --approval-policy never --json -`。任务由 stdin 传入，避免 shell 拼接。`never` 只决定审批行为，不授予额外权限。[CLI 帮助](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cli-help.md)
- Headless 单次调用结束即退出，支持 JSON 行事件。持久 Session 恢复需要沿用原 `DSH_HOME` 和目录；未知 ID、目录或 preset 不匹配会失败。本项目禁止自动换新 Session 掩盖恢复失败。[Headless 说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)
- 适配器核对进程退出码、唯一的开场 `session`、结尾 `final`、相同 `sessionId` 和 `reason.kind == completed`；不兼容的旧格式先阻塞。模型声称“测试通过”不能满足这些检查，更不能替代外层测试。[JSON 事件实现](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/src/json-stream.ts)
- 官方 Python SDK 使用 stdio JSON-RPC，提供 `resume_session` 等接口；本版为减少依赖选用 Headless。SDK 的 `max_tokens` 约束单次请求输出，并不是整个任务的费用上限。[Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)

## Windows 边界

Windows ACL 沙箱对读取、硬链接等行为仍有局限，不能把 `workspace-write` 当成完整机密隔离。Windows 子进程目前拒绝额外 writable roots，因此采用独立 clone，避免 worktree 共享 `.git` 目录需要额外授权。先在一次性专用 VM/独立主机和无个人凭据的低权限账户验证；GPU 任务需要另外验证 GPU 可用性。[沙箱限制](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-local/README.md)

Headless 默认不使用 GUI 保存的授权，API key 应由独立执行账户的进程环境注入。环境白名单剔除 GitHub/AWS 等发布凭据，只传 API key 及必要系统变量。`DSH_HOME` 必须独立，工作区有 `.env` 会被拒绝。注意环境变量过滤不限制同一账户读取磁盘凭据；专用主机/VM 和分离发布账户仍必需。

`DshRunner` 要求显式 `authorized=True`，并要求记录经过操作者核对的模型、版本及外部硬额度。`model_identity_source=operator_verified_profile` 表示模型来自已核对 profile，CLI 事件本身不提供可靠的模型版本证明。修改 profile 后必须重新核查。

CLI 帮助未提供整个任务的总金额硬上限。本地实现的超时、日志上限、重试次数只能限制运行，不能保证金额；必须使用供应商已验证的硬额度，或专用有限额的网关/账户，才能将预算项置为已验证。没有这个设施应保持 BLOCKED。

## 安装与只读检查

官方仍将项目标为 developer preview，接口可能变化。安装时选定精确版本并保留 lockfile，升级后重新核查 help、事件和沙箱；不要用未固定的 `npx` 在工作开始时隐式下载最新版。[官方仓库](https://github.com/deepseek-ai/deepseek-harness)

已有桌面版可通过应用菜单的 Manage dsh Command 注册其 CLI（这一步需要用户另行同意）。它使用桌面安装的 runtime，窗口关闭后仍可使用。[桌面 CLI 说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)

另提供 `scripts/Install-Harness.ps1 -Version <精确 npm 版本>` 的默认预览。经授权后加 `-Execute -Repository owner/repo -AuthorizationFile <文件>` 才安装官方 npm 包，依赖生命周期脚本默认关闭。若该版本需要 lifecycle/native build，安装仅下载文件，保持阻塞，先审查依赖再作部署调整。本轮没有验证任何安装结果。

适配器配置示例（仅示意，均不是有效授权）：

```json
{
  "command": ["C:\\Program Files\\nodejs\\node.exe", "C:\\dsh-lab\\tools\\node_modules\\@deepseek-ai\\dsh\\实际官方入口.mjs"],
  "home": "C:\\dsh-lab\\dsh-home",
  "timeout_seconds": 3600,
  "max_output_bytes": 16777216,
  "model": "填写在官方 profile 中实际验证的 model ID",
  "verified_version": "填写已执行 --version 核对的版本",
  "budget_enforced_externally": false
}
```

安装后检查包的 `bin` 指向，不要照抄示例入口。为了不启用 cmd shell，适配器拒绝 `.cmd/.bat` shim；Windows npm 安装使用原生 `node.exe` 加官方 JS entrypoint。仅执行 `--version`、`--profile headless --help` 核对接口，不附带任务文本。`doctor` 自身只检查路径，不自动启动 DSH。

## 进程与日志

`process.run_process` 默认不经 shell，stdin 传 prompt，Windows 使用 owned Job Object 在超时和退出时清理后代进程。日志合计默认 16 MiB，超限停止运行。API key 值在跨读取块边界也会被替换；日志仍可能含源码、个人信息或未知密钥，禁止直接上传原始 stdout/stderr。公开证据仅包含结构化摘要和经过审查的测试日志。

Windows Job 关闭导致的终止可能返回退出码 0，因此超时标记和输出超限标记必须独立检查。本模块任一标记为真都拒绝成功。Job 在 Python 启动进程后立即加入，不能作为防恶意代码的启动隔离保证；专用 VM 是部署前提。

## Runner 与开机恢复

`scripts/Inspect-Windows.ps1` 仅读 OS、权限、PATH、磁盘和 `nvidia-smi`/`nvcc` 版本。CUDA 编译器未安装不影响普通 CPU 任务；`nvidia-smi` 显示驱动兼容性不等于项目 CUDA 构建已验证。

`Initialize-Windows.ps1`、`Install-Runner.ps1`、`Register-Runner.ps1` 默认只返回计划。所有变更需要授权文件匹配仓库、未过期、包含 `deploy` scope 且 `isolated_environment_verified: true`，并拒绝在管理员进程内运行。授权文件必须由用户明确同意后另行提供，本轮不创建有效文件。

Runner 安装输入必须来自仓库 Settings → Actions → Runners 页面：精确版本和 SHA256。注册时临时 token 仅通过 `ACTIONS_RUNNER_INPUT_TOKEN` 进程环境提供，脚本在 finally 中移除，不写命令行/配置。这个环境输入接口来自[官方 runner 实现](https://github.com/actions/runner/blob/main/src/Runner.Listener/CommandSettings.cs)。注册不会启动 Runner，也不会安装服务。

Windows Runner 支持 64 位 Windows 10/11/Server，并通过出站 HTTPS 443 联系 GitHub；无需公开入站端口。Runner 离线时 job 排队，超过平台限时失败，恢复后应按相同任务 ID 重建而非创建新任务。[系统与队列要求](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)

需要开机运行时，管理员应在独立环境内按 GitHub 当前页面完成服务注册，明确设置专用低权运行账户，并在 Windows 服务属性的“恢复”页设置失败重启；不要让服务以管理员或 SYSTEM 执行 Agent。管理员仅安装服务，随后退出管理会话。脚本不自动创建账户、保存密码或安装高权限服务。先验证机器重启后服务身份、网络和额度边界，再开放事件调度。[Runner 注册与 Windows 服务要求](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
