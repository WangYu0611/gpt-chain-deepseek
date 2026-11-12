# 本轮 DeepSeek 单请求执行方式

本轮只实现固定算术任务的真实集成探针。DeepSeek 通过官方 Harness Headless 输出函数源码；受信控制器随后解析 AST、应用允许的改动、运行真实测试并发布 PR。模型没有文件、终端、Web 或 GitHub 工具。这不等于已经完成通用无人值守代码 Agent 的部署。

## 已实现的本地接口

```python
from pathlib import Path
from lab_harness.bounded_dsh import run

result = run(prompt, Path(workspace), Path(fresh_output), {
    "desktop_install": r"C:\Users\wy331\AppData\Local\Programs\DeepSeek Harness",
    "credentials_path": r"C:\Users\wy331\.dsh\.credentials.yaml",
    "verified_version": "0.2.1-rc.3",
    "model": "deepseek-flash",
    "timeout_seconds": 120,
    "usage_authorization": {
        "source": "explicit_user_request",
        "scope": "single_issue_smoke",
        "user_text": "随便使用",
    },
})
```

示例中的凭据路径不包含密钥。Python 包装器仅检查该文件存在，不读取、打印或复制它。官方账号服务经公开 `credentials.path` 配置读取已有 Desktop 登录；没有自建账号 HTTP 接口。若实际账号未登录或模型无资格，调用应报错，由用户在官方 Desktop 完成登录。

结果含 `session_id`、`final_text`、`request_count`、`usage`、`model`、`version`、`stdout_path`、`stderr_path`、`guard_report_path`、`normalized_events_path` 和 `process_result_path`。每次启动先用官方 `--version` 重验安装版本，发生自动升级即拒绝发送并要求重新检查协议。`cost_amount` 为 null：没有声称供应商总金额硬限。原默认 `DshRunner` 和主 CLI 不受本模块影响。

## 执行边界

每次调用创建独立、全新的 `DSH_HOME/profiles/bounded-smoke/`。profile 的 `bundles` 为空，完整树只包含官方 Agent/LLM/Session 核心、Headless runner、账号/凭据 provider、Session JSONL 持久化和本地受信策略插件。它不继承用户或桌面 profile，不挂载 shell、文件工具、MCP、Web、subagent、title LLM、compaction 或 retry。

`live_tools/one_request_guard.mjs` 使用官方公开扩展点：

- `agent/created` 设置空工具 allow mask，工具 guard 拒绝所有调用。
- `agent/pre-step` 独立检查可见工具集合必须为空。
- `agent/request` 固定账号 provider、`deepseek-flash`、关闭 reasoning 和 512 输出 token；拒绝第二次 Agent 请求。
- `llm/stream` 在所有模型消费者的共同入口再检查一次：整个进程最多 1 次模型请求、输出上限 512、完整输入消息序列最多 8192 UTF-8 字节、仅文本消息、零工具。检测到第二次调用立即拒绝。
- Python 限制任务 prompt 最多 4096 UTF-8 字节、子进程最多 120 秒、stdout/stderr 合计受现有进程层限额约束。不会自动重试模型请求。

Headless runner 显式注入 `boundedSmokePolicy` 服务，保证先装策略再启动 Agent。Guard 自己写本地机器报告，报告只记录计数、限制、usage 和失败原因，不保存凭据。进程成功还必须通过旧版协议的 session、turn_start/turn_end.completed、final 和 exit 0 检查；出现工具事件也会拒绝结果。

本机账号 provider 会把一条模型目录成功加载消息写入 stdout，首次真实执行暴露了它与 NDJSON 混排的兼容问题。现在仅识别官方固定 origin 和精确格式的这条成功 metadata，写入独立 `normalized-events.jsonl` 再解析；任何其他非 JSON 行均拒绝，原始 `stdout.log` 不改写。进程一退出就先保存 `process-result.json`，包括退出码、超时和日志上限状态，因此后续解析失败也保留外层进程证据。

这些限制适用于本轮工具为零的固定任务。通用文件修改、执行项目代码、任意 Issue 领取或长期 Runner 仍需要独立执行账户、操作系统与网络隔离以及合适的费用控制。

## 本机已执行的验证

- `tests/test_bounded_dsh.py`：15 项全部通过，其中 JS 策略测试在官方 Desktop 的 Node 模式真实执行，覆盖第二请求、工具、输出上限、非文本和超长输入拒绝。真实 stdout 派生 fixture 另外覆盖 provider metadata 混排、原始日志保留、未知文本拒绝及解析失败后进程证据持久化。
- `.runtime/live/bounded-preflight-1/`：使用合成空凭据，真实启动本机官方 CLI 的完整干净 profile；到达 pre-step 后主动拒绝，`request_count=0`，`visible_tool_count=0`，没有账号/模型网络请求。CLI 的 `exit 1` 和 `turn_end.reason.kind=blocked` 是此次拒绝式预检的预期结果。
- 本说明写入时没有由该子任务实际调用模型。真实模型和 Runner 证据由主控制流程记录。

## 费用语义与来源

用户已明确允许本轮模型用量。这里只设置可核验的请求次数、输入大小和输出 token 上限，不把这些限制称为人民币硬额度，也不宣称 Desktop 调用免费。官方定价按实际输入和输出计费，并优先扣除赠送余额；当前 Flash 的高峰价为每百万输入未缓存 token 0.30 美元、输出 token 1.20 美元。正式账单以供应商记录为准，详情见 [DeepSeek 官方模型与定价](https://api-docs.deepseek.com/quick_start/pricing/)。

接口依据为实际安装 0.2.1-rc.3 中的 `dsh`、`dsh-headless`、`dsh-tools`、`dsh-agent`、`dsh-llm`、`dsh-credentials-local` 和 `dsh-llm-deepseek-account` 官方 README 及公开编译实现。实际调用始终使用官方 Desktop CLI；本地策略插件没有替代模型客户端或自行处理账号凭据。
