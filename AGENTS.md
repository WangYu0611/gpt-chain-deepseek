# 项目运行约定

- 使用 Claude API Key 时，使用 `claude-fable-5-2`，思考强度为 `high`，覆盖规划、编码、美术及全流程测试等 Claude API 调用。
- 项目外部接口请求统一通过新加坡节点。主程序和美术 worker 通过 `network_file` 共用本机 `network.local.json`，当前专用代理为 `http://127.0.0.1:17907`，出口国家为 `SG`。
- 代理来自当前 Clash Verge 配置中的固定新加坡监听器。切换订阅或修改网络配置后，重新验证出口国家与接口调用，再恢复任务。
- 密钥从 Git 忽略的 `secrets.local.json` 读取。
