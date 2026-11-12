# 本机官方 DSH 实际预检

本页保留调用前预检。后续已使用受限单请求适配器真实运行官方 DSH 并由 Runner 自动发布 PR，见 [真实流程记录](REAL_ISSUE_DEEPSEEK_PR.md)；通用执行隔离仍未验证。

日期：2026-10-28（Asia/Shanghai）。用户已授权创建 GitHub 测试仓库并尝试联动。本记录中的模型执行阻塞来自环境条件未验证，不是缺少本次测试授权。

本轮仅运行官方 CLI 的 `--version`、`--help`、`--profile headless --help` 和 `--dump-default-config --profile headless`，读取安装包内公开 README / CLI 实现。未提交提示词、未调用模型、未读取凭据文件、未检查私有账号余额、未改变安装或用户配置。

## 实际安装与版本差异

现有官方 shim 位于 `C:\Users\wy331\AppData\Local\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd`。它设置 `ELECTRON_RUN_AS_NODE=1`，通过官方桌面程序的 Node 模式运行包内 `dsh-desktop-host/lib/cli.js`。这是真实 CLI 入口，无须启动桌面交互窗口。

- 实际版本：**0.2.1-rc.3**；`--version` 退出码 0。
- 已核实 Headless 选项：`--json`、`--session-id`、`--help`；帮助退出码 0。
- 此安装不提供 `--permission-mode` / `--approval-policy` 命令行选项。权限来自 `DSH_PERMISSION_MODE` 和官方配置 overlay，不能直接复用新版本的参数。
- 开场 `session` 包含 `sessionId` / `cwd`；旧版 `final` 只含 `type` / `text`。真实结束原因在 `status`、`phase=turn_end`、`reason.kind`，进程退出码也由这个结束原因决定。需要这两项机器证据，不能仅接受 final 文本。
- 官方原生调用前缀包含 Electron 的 `--expose-internals` 和包内入口；默认 `DshRunner` 限定的启动形式、环境白名单和新 JSON 协议均与此版本有差异。

新增 `lab_harness/dsh_legacy.py` 独立适配此版本，不修改默认适配器，不接入 CLI 实际执行。它核验开场身份、配对的 turn_start/turn_end、最终 completed 原因、final 和退出码，拒绝缺失、截断、错误、会话错配及已完成后仍有未结束新回合。调用仍需真实隔离和外部硬预算条件。

## 账号与最小调用条件

安装包的 Headless README 明确支持以 `--patch` overlay 重新启用 `credentials`、`authorization`、`deepseek-account`；现有项目 `dsh-account.patch.yml` 使用这一公开机制并选择 `deepseek-account` / `deepseek-flash`。该文件不含凭据。当前预检没有验证保存的账号是否已登录、是否有模型资格或剩余额度，因此不能声称模型调用已具备条件。

默认 Headless profile 禁用上述三个服务，默认路由是 `deepseek-official`。进程环境没有 `DEEPSEEK_API_KEY`。这不代表用户没有桌面登录，只表示 API-key 路线当前未配置。

本版默认 workspace-write 对应 approval ask；无人值守应通过受信调用 overlay 将 approval policy 设为 never，沙箱保持 workspace-write。`never` 会拒绝需要升级权限的操作，不授予额外权限。兼容适配器生成的调用 overlay 也关闭 `llm-retry`，避免一次小测试因供应商错误自动重试。

官方 `llm-retry` 文档说明默认普通模式最多自动重试五次，每次可能计费。`modelOverrides.deepseek-flash.maxTokens` 可以限制每个请求的输出，但不是总输入、总调用次数或总费用硬上限。CLI 没有已核实的任务总费用选项。最小真实探针仍应满足：专用隔离执行环境、外部已核实的硬额度、固定模型的小输出限制、关闭自动重试、短进程超时、唯一测试任务，且所有日志留本地。

## 当前真实阻塞

1. 保存的桌面账号可用性和模型资格未验证；本次未调用账号服务。
2. 没有已验证的供应商/网关总费用硬上限，不能把 maxTokens 或超时宣称为金额保证。
3. 现有 Windows 用户环境未证明秘密读取、网络出站和执行目录隔离；仅设置 workspace-write 不够。
4. 默认 Worker 仍未接通真实受控执行；兼容适配已提供，但未以真实模型验证。

机器记录：`.runtime/live/preflight-dsh.json`。兼容协议和默认拒绝执行测试只使用本地事件文件与假进程。真实 GitHub 与 CI 联动结果由主任务单独记录。

## 依据

优先依据实际安装内的 `@deepseek-ai/dsh-headless/README.md`、`lib/json-stream-BA-F3lfb.js`、`dsh-desktop-host/lib/cli.js`、`dsh-llm-retry/README.md`、`dsh-llm-deepseek-account/README.md` 和 CLI 帮助。在线源码对应的 [官方 Headless 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md) 可能已更新，不应覆盖本机版本实测结果。
