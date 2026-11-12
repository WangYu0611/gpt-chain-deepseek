# Codex × dsh 单机无人值守

当前重构入口为 **`unattended.cmd`**：Codex 发布任务书并验收，dsh 编码、测试并提交；单机、单项目、串行推进，任务和运行阶段持久化，重启后从原断点恢复。

默认按北京时间运行：工作日 **18:00 至次日 08:00**，所有周末（包括调休周末）及配置日历中的节假日全天。08:00 默认严格停止当前步骤，保存文件、提交和会话断点，在下一工作窗口恢复；也可手工选择限时收尾策略。任务在独立 Git 副本和工作分支执行。

未配置 `publication` 时，`push` 控制逐任务发布，默认只在本地提交。配置 `publication.mode: "milestone"` 后，所有单项任务均留在本地；完整阶段完成提案经过重新执行独立检查和 Codex 阶段验收后，才幂等推送工作分支并结束，不自动进入下一阶段。阶段发布模式本身授权这次推送，即使 `push: false` 也会在阶段验收通过后推送。配置示例中的 M2 目标须按实际项目完整范围替换，不代表阶段已完成。

```powershell
.\unattended.cmd init --project "C:\Projects\my-project" --goal-file "C:\Projects\requirements.txt"
# 编辑 unattended.local.json，为 checks 填入项目实际的验收命令。
.\unattended.cmd doctor
.\unattended.cmd run
```

Windows 后台运行使用 `unattended.cmd background start`；`background install` 注册当前用户登录后自动恢复。查看、暂停、继续分别使用 `status`、`pause`、`resume`。配置和检查不会调用模型；`run` 与 `background start` 会按工作窗口执行真实任务。

双击 **`打开运行面板.cmd`**，或运行 `unattended.cmd dashboard`，打开 [本机运行面板](http://127.0.0.1:8765/)。每 3 秒更新当前任务、执行方、模型档位、活动摘要、检查结果和验收历史。面板服务独立运行并随当前用户登录恢复，关闭网页不影响开发任务；面板本身不调用模型。

面板分为总览、功能开发、美术工作室、验收记录和运行设置。总览显示当前开发任务与待验收美术预览；[美术工作室](http://127.0.0.1:8765/#art) 提供大图及视角切换、本批累计费用、技术检查摘要和版本验收。自动刷新保留当前页面与同版本的验收草稿，新版本需要重新填写意见；费用未核实或未结清时可退回修改，暂不能接受。后续未授权预算的批次仅显示计划。

独立美术执行器可配置 `art_autonomy: {"enabled": true, "max_cycles": 3, "rework_window": 3}`。常规返工到阈值后，系统自动让真实 Claude Opus 5.5/high 在原制作会话的只读分支独立诊断、提出调整计划，不预设美术解法。在已确认风格、预算、批次、工具与验收权限内，计划原样交回原制作会话执行；需要改变授权或持续无进展时保留原因与证据。自动窗口单独计数，不冒记人工批准。作品仍须独立技术审核及用户视觉接受后才能入库，功能开发不使用这项策略。

功能开发可单独配置 `task_replanning: {"enabled": true, "max_cycles": 3, "rework_window": 3}`。连续返工到阈值后进入 REPLAN，由配置的 Claude 关键规划模型重新分析当前任务；新计划交回原编码会话执行，不替换原任务、验收条件或核心测试，也不提前接受提交。已有 REVIEW 阻塞可使用 `unattended.cmd retry --replan` 恢复到这一路径。持续无进展和确需外部条件的任务保留证据并停止，不无限扩展返工次数。

编码可用 `code_timeout_seconds` 单独设置有限总预算，未配置时沿用 `step_timeout_seconds`。启用同任务重规划后，真实 CODE 预算中断会保存原生回执、原会话和未提交代码指纹，再交给关键规划；它不代表代码验证失败或本轮交付完成。旧版本遗留的同类阻塞也会依据真实回执恢复。普通错误、暂停、证据不符及重复无进展继续保留原门禁。新调用只展示当前状态，旧超时保留为历史记录。

更新主后台可运行 `unattended.cmd background reload`，通过 `background reload-status` 查看切换进度。重载等待当前调用完成或按原有预算返回后的暂停边界，不改正在执行的调用时限；确认新后台健康后才解除本次维护自己的暂停标记。人工暂停始终保留。

[功能开发页](http://127.0.0.1:8765/#development) 展示当前任务、六阶段流水线、节点详情、独立检查矩阵和最近验收记录。点击阶段查看模型分工，或选择“跟随当前阶段”；轮询保留手动选择。检查只计真实结果，上一轮证据单独标明，本次调用用时不代表任务总耗时。手机上流程自动重排。

飞书简报使用互动卡片：**功能开发统一蓝色，模型验收统一红色**，通过、返工和异常由标题与状态文字区分。私有机器人配置保存在 `.unattended/feishu.local.json`，不进入源码或网页 API。开发通知包括工作窗口开始/结束、任务验收通过及阻塞/后台异常，普通轮询与功能任务单次返工不刷屏；模型通知包括固定版本待视觉验收、用户通过/返工意见及制作阻塞，按任务和验收版本去重。失败退避重试，投递记录可在面板查看。面板地址仅本机访问，手机通过飞书接收卡片。已有显式 `events` 列表的安装需追加 `art_review_ready`、`art_accepted`、`art_rework`、`art_blocked`；缺省列表自动包含这些事件。

需要先连续验收几项任务时，可运行 `unattended.cmd run --tasks 3`。本次明确允许窗口外执行时，加上 `--ignore-window`；它只作用于这一批，保留原工作时间配置，不注册新后台。中断后，相同批次命令、普通 `run` 或登录恢复都会先补完原固定目标；普通 `run` 完成该批后恢复原时段常规运行，暂停或阻塞时保留批次目标。`run --once` 有未完成批次时只推进其中一个步骤，不越过批次边界。已完成批次命令重跑只返回原结果，历史记录不会阻止之后正常运行。

详细配置、节假日日历、后台日志和恢复方法见 [无人值守使用说明](docs/UNATTENDED.md)，配置结构见 [配置示例](unattended.example.json)。离线演示使用 `unattended.cmd demo --output "C:\Temp\codex-dsh-demo"`，不会调用模型或访问远程仓库。离线验证通过不代表真实项目已完成持续运行验收。

## 本机密钥配置

```powershell
if (!(Test-Path secrets.local.json)) { Copy-Item secrets.example.json secrets.local.json }
```

打开 `secrets.local.json`，填写启用服务的密钥。使用 `--config` 指定其他目录下的无人值守配置时，把密钥文件放在该配置旁边。本仓库的 Git 忽略 `secrets.local.json`。

| 字段 | 使用场景 |
| --- | --- |
| `CODEX_API_KEY` | `codex.auth_mode` 设为 `api_key` 时用于派工与验收。 |
| `DEEPSEEK_API_KEY` | 使用 DeepSeek API 的 dsh 配置。 |
| `TRIPO_API_KEY` | 在美术批次的 `key_file` 中填入 `secrets.local.json` 的绝对路径，该文件须位于目标项目目录之外。 |
| `FEISHU_WEBHOOK_URL` | 启用本地飞书通知且通知设置未填写 `webhook_url` 时使用。默认在 `.unattended/feishu.local.json` 中设置启用状态、事件与面板地址。 |

Codex 和 DeepSeek 优先使用进程环境中的同名变量。Codex 订阅模式与 Claude 使用各自 CLI 的登录。GitHub 推送使用 Git 已配置的凭据。

## 历史入口：GitHub × DSH 配置

双击项目目录中的 **`打开GitHub仓库设置.cmd`**，打开本地设置窗口。也可以运行 `github-harness.cmd`。

1. 填写 GitHub 仓库地址、默认分支和允许派工的 GitHub 账号。本地项目目录、项目说明可选。
2. 点击 **保存配置**。配置写入 `github_harness_lab/config.local.json`，状态为“待接入部署”。
3. 点击 **生成通用部署包**，得到同一套通用执行器、该仓库的 `project-profile.json`、禁用工作流和两份网页任务提示词。更换仓库只改配置，不另写执行器。
4. 切到 **接入与使用**，点击 **保存并复制接入请求**，粘贴回当前 Codex 对话，核对并审阅部署。新仓库完成工作流、Runner 和独立网页任务配置，并获得本次启动授权后，才能运行自动闭环。

当前通用执行器支持两项受限文档任务和第三项只发布，不代表任意代码开发已实现。窗口支持本机只读检查；保存或导出不会发 Issue、调用 DeepSeek、注册 Runner 或改变 GitHub 权限。上次已跑通的测试仓库目前已停止，换地址后需要为该仓库完成部署，不能沿用旧授权。详见 [GitHub 试验区说明](github_harness_lab/README.md)。

## 原有桌面传话工具

以下保留原有 `desktop-relay.cmd` 工具的说明。该入口会模拟网页/桌面输入，与上面的 GitHub 配置入口独立。

启动一次后自动循环：**通知网页 GPT → GPT 审阅并发布任务书 → DSH 指定项目新建任务卡 → DSH 完成交付 → 再通知 GPT**。正常交接不需要输入 `g` 或 `d`。

脚本只读取两个应用的界面状态并模拟输入，不访问 GitHub，不调用 Git，不使用模型 API。代码拉取、审阅、交付和上传由 GPT / DSH 自己完成。

## 启动

当前电脑已经安装依赖。换电脑时先安装 Windows、Python 3.10+，然后在工具目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
```

保持已配置的 GPT 对话和 DSH 窗口打开，停留在对话底部、输入框为空、布局与校准时一致。先只读确认两边状态可读取：

```powershell
.\desktop-relay.cmd probe
```

两边 `available: true` 表示能读取当前界面；`busy: true` 表示正在工作，`idle: true` 表示确认输入框空白且没有运行按钮。草稿、待批准或读取不完整时不会交棒。`probe` 不点击、不输入、不发送，首次需要在本机实际运行它验证界面可读性。

正式启动：

```powershell
.\desktop-relay.cmd run --send
```

上面是前台模式，需要保持 PowerShell 窗口运行。独立后台模式使用：

```powershell
.\desktop-relay.cmd background start
.\desktop-relay.cmd background status
.\desktop-relay.cmd background stop
```

后台启动由 Windows 任务计划程序在当前已登录用户的桌面会话中执行，独立于本次聊天的命令进程；不创建定时或登录触发器，不保存密码，不使用 SYSTEM 或提升权限。状态命令核对任务状态、进程和心跳；日志保存在 `.window-relay/auto-run.log`、`.window-relay/auto-run.err.log`。停止后再次启动前，用 `resume` 解除暂停，原轮次和识别码保持不变。不要同时运行前台与后台实例。

后台任务使用微软文档中的 [InteractiveToken 登录类型](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskfolder-registertaskdefinition)，并设置[不限制运行时长](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-executiontimelimit)。

首次默认先通知 GPT；此后完全自动交接。若 GPT 已发布任务书，首次想先交给 DSH，可用：

```powershell
.\desktop-relay.cmd run --send --start-with dsh
```

`--start-with` 只在没有自动循环断点时决定第一棒。重启会继续等待原来那一棒，避免重复发送。已有旧版本进程时先 `Ctrl+C` 退出，再启动新版。

去掉 `--send` 只预览首次正文，不读取界面或发消息。`--once` 只检查一个状态步骤，通常无需使用。

## 怎样知道处理完了

脚本每轮在固定指令后附加一份自动交接约定，要求对方真正完成工作后，在最终回复输出带本轮随机识别码的完成回执。识别码每轮不同，提示词本身不包含完整回执字符串，避免把刚输入的用户消息误当回复。

- GPT 只有在审阅结束并发布了可执行的下一任务书或修正任务书后，才能报告完成。
- DSH 只有在任务及报告、证据等交付上传完毕后，才能报告完成。
- 观察到本轮完成回执、停止生成/后台运行按钮消失、输入框空白，并连续多次保持稳定后，脚本自动通知另一方。
- 旧回复、仅看到空闲、仅发送动作返回都不会触发交接；模型不按约定输出回执时继续等待，超时后暂停。
- 项目已全部完成、没有下一任务、等待用户批准、工具报错或需要人工决定时，对方应输出阻塞回执，循环暂停。

这个回执由 GPT / DSH 自动生成，你不需要每轮手工登记。任务完成与发布内容由两端负责，脚本不验证 GitHub 内容。

双方每轮都直接使用实际任务编号，例如 `TASK-M2-20`，不在前面添加 `task+` 等额外前缀。GPT 新任务书或修正任务书的标题、正文第一行，以及最终回复中的下一任务标识行，都从实际编号开始，例如 `TASK-M2-20 下一任务：具体内容`；DSH 的交付报告标题、正文第一行和最终任务标识行也采用同一规则。两端的 commit 标题必须仅为实际任务编号本身，例如 `TASK-M2-20`，不附带 `task+`、说明文字或其他前后缀。示例编号不能代替实际任务编号。任务编号用于识别任务，每轮随机完成回执仍单独负责触发交接。

## 配置与校准

`desktop-relay.json` 已保留你的 GPT 地址、`ue_game` 项目和 DSH 程序路径。当前 `sender_mode: "uia"` 使用 Windows 无障碍控件定位新会话和发送按钮；输入后完整回读，核对成功才调用发送。它不再把鼠标点击接口返回成功当成消息已发出。

两端 `uia.write_mode: "wm_char"` 把文字输入到已核实焦点的 Chromium 编辑器，并把原文换行替换为空格；发送通过真正的 Send / 发送消息按钮完成。不会用回车提交。DSH 在建卡前后都核对当前选中任务属于配置的项目，创建空白新卡后才写入。GPT 每次发送前核对地址栏对应配置的对话。两端都保留不确定事件记录，禁止盲目重发。

每张 DSH 新卡发送前会按 `dsh.uia.access_mode: "完全权限"` 选择并确认权限模式；已经是完全权限时直接核对，不重复切换。核实权限菜单关闭及模式生效后才输入，发送前再次核对。当前配置不会选择自动审查，也不会处理它的启用确认。未能确认模式或出现未知审批弹窗时不会输入或发送任务。这个配置要求使用 `sender_mode: "uia"`。如果日后明确配置为“自动审查”，才会使用对应的模式及其专用确认流程。

如果点击新建或发送时旧界面控件失效，脚本只重新读取界面来确认操作结果，不重复点击。只有确认配置项目下选中的新卡、正确权限、完整消息及发送后的界面回执，才继续下一阶段；无法确认时保留原轮次和记录。

DSH 项目检查使用当前客户端实际暴露的侧栏分组结构；`dsh.uia` 中的编辑器类名、新卡名称和行类名后缀都可配置。客户端升级后若控件匹配失败，脚本会停止，需根据 `probe --diagnostic` 更新配置。`sender_mode: "native"` 保留旧坐标输入方式。

需要改动点位时：

```powershell
.\desktop-relay.cmd windows
.\desktop-relay.cmd calibrate --target gpt --hwnd <GPT窗口编号>
.\desktop-relay.cmd calibrate --target dsh --hwnd <DSH窗口编号>
.\desktop-relay.cmd doctor --completion
```

用 windows 输出的实际数字替换尖括号。校准时按 Enter 后有 5 秒移动鼠标到目标位置，脚本只记录坐标。DSH 新任务步骤由你手动演示，不要录入权限或模型设置。

GPT 使用专用浏览器窗口，选中指定对话标签。DSH 标题匹配使用稳定部分 `DeepSeek Harness`。DSH 保持打开本轮正在执行的任务卡；切到历史任务会令脚本等不到本轮回执。运行时不要在目标输入框写草稿。窗口大小、DPI、网页缩放或侧栏改变后需重新校准；语义模式仍核对已记录的窗口尺寸和 DPI。

`auto` 默认设置：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `start_with` | `gpt` | 首次先通知谁 |
| `poll_seconds` | `3` | 界面检查间隔，不是交接定时器 |
| `stable_samples` | `3` | 至少连续三次稳定 |
| `settle_seconds` | `5` | 完成回执至少稳定五秒 |
| `phase_timeout_seconds` | `21600` | 每棒最多等待六小时 |
| `unavailable_timeout_seconds` | `60` | 界面持续不可读时暂停 |

`messages.gpt` / `messages.dsh` 可改固定正文，自动循环会在它后面附加本轮回执约定。窗口按钮和输入框名称随版本或语言变化时，可在各端 `completion` 配置 `busy_regex`、`blocked_regex`、`editor_regex`；默认支持中文和英文停止/发送按钮及 DSH 后台运行状态。DSH 常驻预览后台任务也可能令脚本继续等待，需根据实际按钮名称调整，不能直接把持续运行当成完成。

如果 `probe` 显示不可读或找不到编辑器，先检查目标标签、布局和窗口匹配，必要时用 `.\desktop-relay.cmd probe --target dsh --text` 查看采集结果。脚本不会把无文本、截断、未知草稿或未识别状态当完成；不要直接启动后假设循环已经验证成功。

`draft_present` 表示检测到未发送文字，脚本会保留内容并等待，**不代表等待审批**。只有匹配到仍可用的审批按钮才会报告 `approval_required`，日志会列出命中的控件。若明明输入框空白却报草稿，用 `.\desktop-relay.cmd probe --target gpt --diagnostic` 查看 `editor_source`、`draft_preview` 和候选输入控件：此选项会显示最多 160 字的文字预览，不会发消息或清空输入框。

已适配 GPT 的 ProseMirror 空输入提示 `Work with ChatGPT`：只有控件和值精确匹配、语音空态按钮均可用且不存在发送按钮等证据一致时，才将它识别为空输入框。`editor_state: placeholder_confirmed` 表示这条规则已命中；诊断仍保留原始提示文字。真正输入的同名文字若出现发送按钮，仍按草稿保留。

换项目可复制 `desktop-relay.example.json` 并修改 `gpt`、`dsh`、`messages`、独立 `runtime_dir`，然后重新校准。配置中的 `repository` 只作备注，不参与执行。

## 暂停与恢复

`Ctrl+C` 退出，`Esc` 或另一终端的 `pause` 会停止循环；查看当前等待的一方：

```powershell
.\desktop-relay.cmd pause
.\desktop-relay.cmd status
.\desktop-relay.cmd resume
.\desktop-relay.cmd run --send
```

Windows 锁屏或交互桌面不可用时，循环输出 `waiting_desktop_unlock`，停止读取应用和发送输入，保留原轮次。解锁回桌面后自动继续，不会自行解锁或修改锁屏设置。若锁屏发生在输入中途，仍按不确定发送保留记录，需核对后恢复，避免重复开卡。

如果发送动作中断，状态保留为 `uncertain`，不会重发。核对网页或 DSH 后选择：

```powershell
.\desktop-relay.cmd resolve --sent  # 确认确实发出，继续等原目标的本轮回复
.\desktop-relay.cmd resolve --skip  # 放弃不确定发送，暂停，绝不自动重发
```

普通退出或 PAUSE 标记会沿断点继续等待。若因阻塞回执、不可读超时或任务超时进入 `phase: paused`，先处理原因，再明确重开一轮：

```powershell
.\desktop-relay.cmd reset-cycle --start-with gpt
.\desktop-relay.cmd resume
.\desktop-relay.cmd run --send
```

也可选择从 DSH 重开。未确认的不确定发送必须先 resolve，不能直接 reset。不要删除状态目录来重试。

自动循环记录在 `.window-relay/cycle-state.json`。主运行锁阻止循环期间同时手动发送。单次 `gpt --send` / `dsh --send` 和可选的 `manual --send` 菜单仍保留用于调试，不是正常循环所必需。

发送现在默认核对两次：输入后读取目标消息框，完整正文匹配才按发送；发送后核对原文已离开输入框或界面已开始响应。无法核对时保留不确定记录并停止，不会盲目再按发送。`request_status: ui_echo_observed` 表示在对话里观察到本轮识别码，与“发送动作执行过”和“任务处理完成”分别记录；超过一分钟仍未看到回显会提示核对送达，不会重发或交给另一方。

完成状态采集通过只读 UIAutomation / PowerShell 子进程进行；语义发送使用 `comtypes` 调用相同界面的输入与按钮动作，不改变执行策略。按用户要求，新 DSH 任务卡设为完全权限。已在当前电脑验证 DSH 新卡创建、M2-20 发送和实际开始执行，以及 M2-20 完成回执识别、通知 GPT 并确认开始审核。持续无人值守的后续交接仍需运行验证；模拟测试不能替代这一验收。
