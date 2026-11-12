# 网页 GPT ↔ DSH 传话器

独立运行在本目录，保留你现在的流程：

`DSH 完成并推送 → 通知网页 GPT 到 GitHub 读取代码、报告、日志和截图 → GPT 审阅并在 GitHub 发布下一任务或返工任务 → DSH 拉取 main 并执行 → 再次审阅`

工具会核对提交和任务文件是否真的发布，并保留交接编号与断点。网页回复本身不能代替 GitHub 发布结果。

## 当前已配置

`config.json` 已填入：

- 仓库：`WangYu0611/zhuguo-city`，分支 `main`。
- 项目目录：`C:\Users\wy331\OneDrive\文档\ue_game`。
- 原网页 GPT 对话和已安装 DSH 的 `resources/runtime/cli/bin/dsh.cmd` 路径。
- 运行数据：本工具的 `.relay`，在游戏仓库外。

传话工具本体没有写入游戏仓库。安装、离线演练和 `doctor` 不启动真实任务；只有你手动运行真实 `run`，才会发送 GPT 消息，并可能由 DSH 按项目流程改代码、提交和 push。`seed` 会访问 Git 远端核对已推送的交付。

后续 DSH 首次在 **CLI 新会话** 中从仓库资料开始执行，再复用这个 CLI 会话；不保证接续桌面 GUI 中已有的任务卡。CLI 使用其实际配置的模型，是否与 GUI 完全一致仍需核实。首次启动前，请确认同一项目的其他 DSH 任务（包括后台任务）已经停止，避免并发编辑。

## 安装与离线检查

在本工具目录打开 PowerShell：

```powershell
.\setup.ps1
.\bridge.ps1 doctor
.\bridge.ps1 run --dry-run
```

安装脚本优先寻找有效的 `py` / `python`（Python 3.10+），必要时使用本机 Codex 或 DSH 附带的 Python，创建 `.venv` 并安装 `requirements.txt`。它不下载 Chrome、不运行 DSH、不更改全局执行策略。需要本机已有 Chrome 和可用 Git。

若系统阻止执行 `.ps1`，可仅对该子进程使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\bridge.ps1 doctor
```

`doctor` 只检查配置、命令和依赖。GitHub 读写权限、GPT 网页选择器、账号登录和 DSH 模型权限仍需实机验证；目前不能宣称真实闭环已经跑通。GPT 原对话必须能够读取仓库中的代码和实际证据，并把任务文件真正发布到 GitHub。默认只发送 GitHub 地址、固定提交与文件路径，无需附件上传权限。

## 首次登录与首轮交付

先打开传话器的独立 Chrome profile：

```powershell
.\bridge.ps1 login
```

在弹出的浏览器中手动登录 ChatGPT，确认已经打开配置中的原对话，然后回终端按 Enter。此 profile 不继承常用 Chrome 的自动登录，登录状态随后保存在 `.relay/browser-profile`；独立 profile 的实际登录和运行尚未验证。

如果 Google 显示“此浏览器或应用可能不安全”，该自动化窗口的登录尚未打通。可以通过已登录的浏览器助手辅助试跑：

```powershell
.\bridge.ps1 run --once --external-browser
```

此模式将网页请求写入 `.relay/external/<交接编号>/request.json`，等待浏览器助手把真实完整回复原子写入指定 `reply.txt`；其他版本核对、DSH 执行和断点记录仍由脚本处理。它需要浏览器助手参与，不是独立无人值守模式，也不能把这条辅助链路通过等同于独立登录兼容已解决。默认传话没有附件，无需为这一步开启 Chrome 扩展的“允许访问文件网址”。

复制交付模板并手动填写：

```powershell
Copy-Item -LiteralPath .\delivery.example.json -Destination .\delivery.json
notepad .\delivery.json
```

| 字段 | 要填的内容 |
| --- | --- |
| `task_id` | DSH 刚完成的任务编号 |
| `base_sha` | **该任务开始前**的完整提交 SHA，不能随意填最终提交的父提交 |
| `commit_sha` | 包含实现、报告和证据，且最终成功推送到 main 的完整提交 SHA |
| `report_paths` | 该提交中实际存在的报告文件；相对于仓库，使用 `/` |
| `screenshot_paths` | 同一提交中实际存在的截图文件；没有时填 `[]`，不能填虚构路径 |

模板中的全零和全一 SHA 只是占位。脚本通知 GPT 从指定 `commit_sha` 读取代码、报告、日志及截图，并按 `base_sha` 到 `commit_sha` 检查本轮完整变化。默认不上传文件；默认必须列出报告，需要强制列出截图时，将 `evidence.require_screenshots` 改为 `true`。

GPT 应实际读取证据内容；截图需要看到图片，只有文件名、链接或 Git LFS 指针不能算已看过截图。仓库、报告、日志或必要截图无法读取时，GPT 必须返回 `pause` 并说明缺少的证据，不能宣称验收通过。

仅在用户明确需要上传文件时，才启用可选的 `evidence.upload_attachments=true`。该模式会将本地取出的报告、代码差异和截图作为附件发送，并受附件数量和大小上限约束。Git LFS 附件必须与已提交指针的大小和 SHA-256 相符；缺少实体时需先在项目目录执行 `git lfs fetch origin <commit_sha>`。使用 Chrome 扩展上传本地文件时，还需根据扩展提示单独开启对应文件访问权限。这些条件不影响默认的纯 GitHub 通知模式。

核对交付后登记并执行首轮：

```powershell
.\bridge.ps1 seed --delivery .\delivery.json
.\bridge.ps1 run --once
```

`--once` 从当前断点继续，完成一次“GPT 审阅 → 发布任务 → DSH 执行并交付”后停下；这次新交付留待下次 `run` 审阅。GPT 要求暂停或宣布完成时也会停止。

## 连续运行、暂停与恢复

```powershell
.\bridge.ps1 run
.\bridge.ps1 status
.\bridge.ps1 pause
.\bridge.ps1 resume
.\bridge.ps1 run
```

运行中的 `status`、`pause` 可在另一个 PowerShell 窗口执行。`pause` 在下一次交接前生效；`resume` 只清除暂停标记，还要执行 `run` 才继续。需要立即中断时在运行窗口按 `Ctrl+C`，再检查日志与远端提交。

- `review_pending`：消息可能已发出。恢复时会用同一交接编号查找原消息，不自动重发。只有你在网页上确认**确实未发送**，才能运行下面的命令，允许同一编号重试一次：

  ```powershell
  .\bridge.ps1 retry-review --confirm-not-sent
  .\bridge.ps1 run
  ```

  此命令不是“重新审阅”按钮，也不会直接发送消息；不要用于已发送但尚未回复的消息。

- `dsh_pending` 且没有完整结果：DSH 可能已启动或已部分完成，工具不会重复派发。核对 `.relay/dsh` 日志、工作区和 GitHub；确认交付完整并已推送后，重新填写 `delivery.json`，用下面的方式接回审阅：

  ```powershell
  .\bridge.ps1 seed --delivery .\delivery.json --replace-state
  .\bridge.ps1 run --once
  ```

  原状态会备份到 `.relay/state-before-reseed.json`，原轮次与 DSH 日志保留。替换状态会重新计数并建立新的 CLI 会话；不能用它掩盖尚未完成的执行。

- 网页改版、登录失效、任务未发布、远端版本不符或未提交改动时会暂停。GPT 无法读取必要证据时也应返回 `pause`；可选附件上传开启时，附件缺失或超过上限同样会暂停。修好原因后根据 `status` 和日志恢复，不要删除 `.relay` 来强行重跑。

- `waiting_user` / `done`：这是 GPT 明确返回的决定，`resume` 不会推翻它。你处理完问题、决定再次审阅时，可以用同一份已核对的交付 JSON 执行 `seed --delivery .\delivery.json --replace-state`，再开始新一轮。

默认最多累计 10 轮、同一任务最多返工 3 次；可调整 `limits`。达到累计轮次上限后，提高 `limits.max_cycles` 才能继续。更换项目或 GPT 对话时，应使用新的 `runtime_dir` 和独立项目工作区，避免串轮。

`.relay/state.json` 保存断点，`.relay/turns` 保存提示词、GPT 回复和 DSH 结果；可选附件上传开启时也保存附件副本。`.relay/dsh` 保存执行事件与错误日志；网页故障截图在 `.relay/browser-errors`。不要公开浏览器 profile 或包含私密项目内容的运行数据。

## 配置与参考

其他项目可从 `config.example.json` 新建配置，并通过 `.\bridge.ps1 --config .\另一个配置.json doctor` 使用；全局 `--config` 参数放在子命令前。`repository.workdir` 是被执行项目，`runtime_dir` 必须在它之外。`browser.selectors` 用于实机适配网页；`dsh.executable` 应指向 CLI，而非桌面 exe。账号复用配置位于 `dsh-account.patch.yml`，不会在此文档中展开凭据。

本机已只读核实 DSH 的 headless 帮助支持 `--json` 和 `--session-id`。配置没有使用较新文档中的权限命令行参数；实际执行若遇到账号、模型或权限阻塞，会暂停并保留日志，需要按所安装版本的 DSH 配置处理。

- [DSH 官方 headless README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)
- [Playwright 文件上传与输入](https://playwright.dev/python/docs/input)
- [Playwright 浏览器启动与持久 profile](https://playwright.dev/python/docs/api/class-browsertype)
