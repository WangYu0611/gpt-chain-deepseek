# 项目运行约定

- 项目运行使用 Codex 专用模式。开发实现使用 `gpt-6.2-sol`，常规规划、关键规划、独立核验和全流程核验使用 `gpt-6.2-astra`，各职责使用独立会话。
- 跨职责默认思考档为 `ultra`。从北京时间 2026-11-22 00:00 开始，新调用永久使用 `high`，由 `codex.effort_change_at` 和 `codex.effort_after_change` 自动执行。
- Responses API 核验可依次使用 `CODEX_API_KEY` 与 `CODEX_API_KEY_BACKUPS`，能力以实际接口返回为准。Codex CLI 使用已登录的 ChatGPT 订阅。
- 项目外部接口请求统一通过新加坡节点。主程序和美术 worker 通过 `network_file` 共用本机 `network.local.json`，当前专用代理为 `http://127.0.0.1:17907`，出口国家为 `SG`。
- 代理来自当前 Clash Verge 配置中的固定新加坡监听器。切换订阅或修改网络配置后，重新验证出口国家与接口调用，再恢复任务。
- 密钥从 Git 忽略的 `secrets.local.json` 读取。
