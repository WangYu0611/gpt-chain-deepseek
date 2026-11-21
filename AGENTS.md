# 项目运行约定

- 项目运行使用 Codex 专用模式。开发实现使用 `gpt-6.2-sol`，常规规划、关键规划、独立核验和全流程核验使用 `gpt-6.2-astra`，各职责使用独立会话。
- 自动运行启用 `headless`。验证使用无交互命令，UE 画面通过正常 RHI 离屏渲染采集。需要实体屏幕或人工操作的验证保留为人工验收项。
- 正式 UE 编译、测试和打包由提交后的独立检查执行。编码阶段提交实现、测试和验证清单，按独立检查回执处理返工。
- 跨职责默认思考档为 `ultra`。从北京时间 2026-11-22 00:00 开始，新调用永久使用 `high`，由 `codex.effort_change_at` 和 `codex.effort_after_change` 自动执行。
- 核验优先通过 Responses API 依次使用 `CODEX_API_KEY` 与 `CODEX_API_KEY_BACKUPS`。密钥全部不可用或 API 在有限重试后仍不可达时，通过已登录的 Codex CLI 订阅继续核验同一提交。
- 项目外部接口请求统一通过新加坡节点。主程序和美术 worker 通过 `network_file` 共用本机 `network.local.json`，当前专用代理为 `http://127.0.0.1:17907`，出口国家为 `SG`。
- 代理来自当前 Clash Verge 配置中的固定新加坡监听器。切换订阅或修改网络配置后，重新验证出口国家与接口调用，再恢复任务。
- 密钥从 Git 忽略的 `secrets.local.json` 读取。
