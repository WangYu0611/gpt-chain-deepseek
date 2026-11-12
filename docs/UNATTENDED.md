# Codex / dsh 单机无人值守闭环

这一入口在一台电脑上管理一个 Git 项目，串行执行规划、编码与本地提交、独立检查、Codex 验收，再按结果返工或进入下一任务。规划可选择 Codex 或 Claude Code，编码可选择 dsh 或 Claude Code。任务书保存在配置指定的运行目录，不依赖 ChatGPT 网页定时任务或 GitHub 事件触发。

## 状态面板与飞书简报

双击工具目录的 `打开运行面板.cmd`，或执行 `unattended.cmd dashboard`。默认地址为 `http://127.0.0.1:8765/`，每 3 秒更新。首屏显示当前任务、下一步、后台心跳和连续失败次数。独立检查区显示通过、执行中和跳过的项目。Codex 卡片显示配置的认证策略，能从当前执行确认时显示实际使用的 API 或订阅。未提供原始时间戳的模型活动显示为“本轮记录”，最近活动时间取日志实际修改时间。

运行面板按流程、职责模块、任务证据与历史记录组织。流程图展示 Sonnet 初拟分级、可选 Opus 定稿、Sonnet 开发、独立检查、Codex 审核及任务登记，阶段审核与发布单独显示。点击流程节点可定位并展开对应模块。六张职责卡片显示各自的模型、当前状态、任务及最近活动。缺少历史模型回执时，配置值会明确标注。返工时保留的旧检查结果标为上一轮，等待新提交后重新检查。暂停、等待额度、心跳过期和连接中断都有独立显示。

面板是只读服务，不启动新开发步骤、不清除断点、不调用模型；仅监听本机回环地址，不向局域网公开。查看面板不会占用 Codex/dsh 模型额度。关闭网页后服务与夜间开发继续运行。独立 Windows 登录任务 `CodexDSHPanel-<配置标识>` 负责恢复面板和简报监测，不依赖浏览器一直打开，也不重启正在编码的引擎。

飞书私有设置文件为 `.unattended/feishu.local.json`，结构为 `enabled`、`webhook_url`、`dashboard_url` 与 `events`。机器人地址只保存在该忽略目录，不写到面板响应或文档。当前已接入用户提供的机器人并成功投递测试互动卡片。所有简报均为卡片，事件包括 `window_start`、`accepted`、`blocked`、`worker_exit`、`window_end`。首次监测建立已验收任务基线，避免补发历史；之后每项验收通过单独简报，开工/收工提供状态摘要，普通轮询、工具调用及单次返工不发送。

通知失败不改变开发状态，最多 3 次尝试并退避；持久投递记录防止服务重启后重复发送。请求中途进程中断时，结果可能未知，系统保留“投递结果未知”并避免自动重发。面板显示最近发送、事件与错误；手机通过飞书卡片了解状态，本机 URL 仅供当前电脑打开。

## DSH 会话隔离与返工恢复

每份新任务书从新的 DSH 会话开始：任务初始 `session_id=null`，headless 调用省略 `--session-id`，由安装版 DSH 生成新 ID。同一任务的返工及程序中断续接使用本任务已保存的 ID，避免丢失未完成修改。`operation.task_id`、任务会话归属和运行目录 `dsh-sessions.json` 共同校验，拒绝跨任务复用或“新会话”请求意外返回旧 ID。旧断点保持兼容。面板当前任务显示实际会话 ID 及所属任务，编码未结束时可从当前操作日志的首 8KiB 获取开头会话记录。

普通 `retry` 不会自动增加返工额度，也不会把验收者的 `blocked` 改成通过。验收缺项能在已有授权、工具和项目范围内修复、重跑或补证时，应返回 `rework`；缺少必须由外部提供的权限、资源或必要决策时才返回 `blocked`，并说明具体条件。

已核实阻塞条件可解除并明确恢复同任务返工时，可执行 `unattended.cmd retry --rework`：核对最新 `rework` 或 `blocked` 验收回执、轮次及当前干净受审 HEAD，使用最新反馈并记录显式恢复审计，保留原始回执。尚有返工额度时使用原额度；已用完时才给当前任务追加一轮人工额度，全局 `max_reworks` 不变。加 `--new-session` 会归档旧会话、开启干净新会话。普通 `retry` 不做这种覆盖，不通过反馈关键词自动改写回执。随后用 `resume` 和 `background start` 恢复，继续原工作时间与独立验收要求；工作窗口外只等待，不调用模型或UE。不得以增加额度替代修复或降低验收标准。

Codex CLI 的已知自动重连进度提示（带次数提示及已核实的等待网络提示）只有在同一回合最终出现完整 `turn.completed`、进程正常退出且结构化结果有效时才可恢复；失败或不完整回合不能被当作成功。普通 `retry` 会重新校验 PLAN/REVIEW 的已保存操作，完整回执与当前干净 HEAD、任务归属一致时保留原断点，无需重复请求模型；不完整或无效的调用仍重新执行。验收仍须绑定当前候选 HEAD，真实的 `rework` 意见交回 DSH，不会当作通过发布。

已有受审候选提交在 CODE 返工中等待外层验证、连续完成但没有新提交时，可用 `unattended.cmd retry --external-validation` 恢复任务级 CHECK。入口核对当前干净候选提交、上一轮返工审核、最近的无提交交接证据和绑定 HEAD 的验证请求，记录请求摘要及恢复审计；同一 HEAD 的相同请求只能恢复一次。此操作保留原会话、提交、返工额度和审核记录，检查后继续独立审核。随后执行 `resume`、`background start`，不需要创建空提交。

用户明确授权保留旧阶段人工待办并推进下一阶段时，可在后台停止、原始配置和状态已备份后调用 `Engine.advance_milestone(next_config, reason)`。入口要求原阶段受审 HEAD 干净、全部独立检查通过，并核对原始审核回执；保留 accepted/deferred 原件，将完整旧里程碑和授权归入 `milestone_history` 及外部审计，新阶段从原序号进入 PLAN。待发布任务和延期门槛按各自阶段计算，旧阶段仍标为人工待确认。该方法仅持久化新状态绑定；调用方须随即保存已验证的新配置，并由新 Engine/worker 加载，配置与状态切换中断时按备份及转换审计补齐同一次转换。旧 Engine 的 provider 不用于继续派工。此操作不发布旧阶段，也不把人工事项改成验收通过。

## 临时服务故障自动恢复

已识别的服务繁忙（500/502/503/504）、临时限流（429）及网络传输故障会持久化为 `provider_retry`，默认等待 3 分钟、5 分钟、随后每 10 分钟重试。没有总重试次数上限；这些故障不消耗真实执行失败或验收返工额度，不会因为连续三次服务繁忙进入 BLOCKED。认证、权限、硬额度不足、上下文超限、未知协议错误及真实未交付问题仍按相应失败流程处理。

DSH 已知的 `STREAM_CLOSED: DeepSeek Messages stream ended before message_stop` 也归为连接暂时中断。分类只取可信终端协议错误，不根据模型正文或工具结果判断；工具结果自身的 `truncated` 标记不影响之后完整终端错误的识别，控制协议截断仍拒绝。空交付会在原任务下一次调用中收到明确的继续实现及提交反馈，不能把“协议结束”当作已完成任务。

Codex 的已知重连进度（包括编号形式的 `request timed out`）只有在进程退出零、单轮完整 `turn.completed` 且结构化结果合法时，才允许消费既有回执，不重新规划或重复调用模型。真正的终止错误、未知错误或不完整结果仍拒绝。无法使用的成功进程回执保留拒绝记录并退役，后续尝试使用新目录，不把同一个旧回执重复累计为多次失败。

Codex 桌面客户端更新会替换 `OpenAI/Codex/bin/<版本>/codex.exe`。执行器在配置指向的旧版本缺失时，仅从同一安装目录发现有效版本，保留模型、思考档位及其他参数；更新期间尚无可用版本或启动瞬间文件被替换时，记录 `local_cli_unavailable` 并按同一退避规则等待。面板显示“等待本地 Codex 恢复”。显式配置的其他自定义程序不会被替换。

Windows 临时拒绝替换断点、心跳或模型回执文件时（WinError 5/32/33），先对已经刷盘的临时文件做最多 2.57 秒的原子替换退避，保留目标文件的完整旧版或新版内容。若文件仍不可用，后台每 30 秒从持久断点重载并继续，不限制此类恢复次数，不消耗任务失败或返工额度。面板显示“等待状态文件恢复”及下次尝试时间，模型不会显示为正在执行；恢复后保留原任务、会话、固定提交和已完成的检查。暂停与工作日历继续优先，后台不会据此清除暂停或扩大工作时间。

等待时后台保持心跳，面板显示“等待服务恢复”、安全原因、尝试次数及北京时间下次重试时间；到期后才实际请求模型。每次尝试有独立日志，保留原任务书、原DSH会话、已有提交和未提交修改，不重新派任务、不把等待冒充成功。等待断点在重启后继续，暂停或工作窗口关闭时仍优先等待，不用 `retry` 或 `resume` 绕过剩余退避时间。模型调用成功后清除当前服务重试状态，按实际阶段结果继续检查与验收。

## 非阻塞的待人工确认事项

用户明确暂缓当前事项时，可执行 `unattended.cmd defer --reason "用户指示与待确认范围"`。仅允许从当前任务的验收阻塞恢复，要求最新同轮验收回执、干净候选 HEAD 以及全部独立检查通过且绑定该 HEAD。程序保留原任务、回执、代码及完整延期快照，将事项加入独立待确认清单，再从下一编号规划其他工作；不会增加已验收数量、推送代码或启动窗口外模型。

2026-11-03 用户手动启动未观察到 HUD 闪动，指示暂缓处理。TASK-LOCAL-0009 因此记录为待手动确认，不再补采连续画面或重复派发闪动诊断任务，继续其他 M2 自动开发。这个观察不等于已自动验证修复；面板单独显示待确认项。后续非关键且需用户观察的事项可在报告中登记，避免把无关的人工观察条件塞入自动任务的硬性验收；核心功能、存档正确性和必要自动回归仍照常验收。阶段最终交付须如实列出待确认事项，不把延期偷换成整个 M2 已通过。

## 首次配置

本机当前由 Claude `claude-sonnet-5-5`、`reasoning_effort: "high"` 负责规划与编码，Codex `gpt-6-astra`、`reasoning_effort: "high"` 负责独立验收。配置位于 `claude` / `codex` 对象中。

Codex 适配器为规划和验收均显式传入 `--model` 与 `-c model_reasoning_effort="high"`。dsh 将档位写入最后叠加的 `agent-default-model.config.reasoningEffort`，并显式覆盖 `llm-deepseek-account.config.thinking: enabled`，避免继承关闭思考的设置；与已有传输重试配置合并。修改后应安全停止并恢复后台，让新进程读取设置。这里的模型设置不会改变已有任务、验收 HEAD、工作日历或 M2 集中推送策略。

在 `codex` 配置中设置 `auth_mode` 为 `api_key`、`subscription_fallback` 为 `true`，派工与验收优先读取本机 `CODEX_API_KEY`。OpenAI API 明确返回余额或计费额度耗尽时，已登录的 Codex ChatGPT 订阅接手同一步，沿用指定模型、思考档位与受审提交。两种认证的日志分别保存。每个新步骤重新优先用 API，临时限流和服务繁忙继续退避重试。

API 与已授权的订阅兜底都因额度不足失败时，原生订阅限额回执会进入 `waiting_provider`，默认每30分钟复查；结构化重试间隔最多等1小时，以便API额度提前补充后继续。后台保持心跳，等待不消耗普通失败次数，也不会重新编码或跳过审核。未带时区的文字重置日期不直接用于调度。认证错误、未知协议错误及未配置订阅兜底的API硬额度失败仍保留原有处理方式。

在工具目录运行，目标目录必须是已有 Git 仓库。目标应包含明确的完成条件；将项目总体需求写入 UTF-8 文本文件后创建配置：

```powershell
.\unattended.cmd init --project "C:\Projects\my-project" --goal-file "C:\Projects\requirements.txt"
.\unattended.cmd doctor
.\unattended.cmd status
```

默认配置文件是工具目录的 `unattended.local.json`。所有命令都可用 `--config "C:\path\project.json"` 指定另一份配置。创建配置、doctor、status 不会调用模型。

创建后先编辑配置的 `checks`，填入项目实际的独立验收命令。默认留空时 doctor 会报告未就绪，`run` 在任何模型调用之前以 `needs_setup` 退出；验收不会仅依赖模型的完成声明。例如 Python 项目可填写以下检查，并将解释器路径换为本机实际路径：

```json
"checks": [
  {
    "id": "unit",
    "argv": ["C:/Python312/python.exe", "-m", "unittest", "discover", "-s", "tests"],
    "timeout_seconds": 600
  }
]
```

命令在独立工作副本中执行，参数按数组传递，不经过 shell。游戏项目应填写适合项目的编译、自动化测试或其他可执行检查；任务中所需的画面验收仍需提供实际视觉证据。

### UE 项目的外层验证

`ue_game` 使用固定 UE 验证桥 `unattended/ue_checks.py`。DSH 在工作区沙箱中编码、报告及本地提交；构建和测试由 CHECK 的正常用户进程执行，避免 UE 全局缓存路径的沙箱写入冲突。DSH 报告在交接时可明确写“待外层独立验证”，最终结果由固定 HEAD 的机器日志和 Codex 审查决定。

使用 Claude Code 时，`claude.ue_preflight: true` 允许编码会话在提交前运行固定的 `ue-editor-build`。可另用 `claude.visual_preflight` 为指定任务配置一条固定的真实渲染命令。提交后仍对固定 HEAD 做独立检查。任务或里程碑的 `ue-editor-build` 失败后，剩余检查记录为 `NOT_RUN`，避免误用上一次的编辑器模块。

可用 `claude.max_output_bytes` 单独调整 Claude Code 的单轮日志上限。本机配置为 64 MiB（67108864 字节），变更在执行器重启后生效。执行日志记录会话、工具名称及状态、额度和完成回执。CC 从工作区直接读取源码，代码历史由每轮的本地 Git 提交保存，GitHub 在里程碑验收后集中推送。

启用 `claude.planned_core_tests` 后，任务书会列出 1 至 5 个对应验收条件的核心测试。`core_tests` 字段仅包含完整测试名，验收条件映射和复用说明放在任务书正文。CC 编译成功后，用调度器为这些测试生成的固定命令逐项验证，再做本地提交。独立检查会在提交上复跑清单中的测试，全量回归安排在里程碑验收。

设置 `planning_provider: "claude"` 可将任务规划交给 Claude Code，配合 `coding_provider: "claude"` 使用。规划使用独立只读会话，输出原有任务书和核心测试清单，编码使用任务自己的会话。Codex 负责逐任务独立验收与里程碑最终验收。未设置 `planning_provider` 的配置继续由 Codex 规划。

本机日常规划和编码使用 `claude.model: "claude-sonnet-5-5"`。配置 `claude.critical_plan_model: "claude-opus-5-5"` 后，Sonnet 在草案中给出规划级别和理由。日常任务直接进入开发，关键系统草案交给独立 Opus 会话核对与定稿后再进入开发。关键规划包括跨系统架构、复杂依赖拆分、存档格式或迁移契约、核心规则冻结与阶段整体规划，单纯要求保留既有契约的任务按实际改动判断。阶段完成提案也交由 Opus 核对，再进入独立检查与 Codex 阶段验收。两种规划均使用 `high` 思考档，关键规划可单独设置 `claude.critical_plan_reasoning_effort`。所选模型和草案来源写入断点及回执，重启与重试沿用已保存的选择。面板在规划期间显示实际所选模型。

最后一次本地提交后，DSH 将请求写到已忽略的 `Saved/Automation/<任务编号>/validation-request.json`，例如：

```json
{
  "version": 1,
  "task_id": "TASK-LOCAL-0002",
  "head_sha": "填写最终完整提交SHA",
  "tests": [
    {"name": "ZhuGuoCity.M2.CatalogGroups", "mode": "nullrhi"},
    {"name": "ZhuGuoCity.Local.CatalogToolGroupScrollVisual", "mode": "normal"}
  ],
  "artifacts": ["Saved/Evidence/TASK-LOCAL-0002/visual-stages.log"]
}
```

请求只能选择完整的项目测试名称与 `nullrhi` / `normal` 模式，不能携带任意 shell 命令或参数。桥核对当前任务、干净 HEAD 和输入文件哈希，要求准确的测试发现数、Success、实际执行数和退出状态；截图及日志必须在该任务的指定目录内并由本次执行产生，验证后备份到外部运行目录。正常 RHI 测试没有截图时，不会因此宣称画面已通过。

任务明确需要更多 M2 或 M3 NullRHI 测试时，可增加 `"test_batches": [["ZhuGuoCity.M2.StorageRules", "ZhuGuoCity.M3.FireResponseHud"]]`。只允许精确的 `ZhuGuoCity.M2.*`、`ZhuGuoCity.M3.*` 测试标识（这里的星号表示命名范围，请求中不能使用通配符），可以混合分组；这不授予新的任务范围。最多 16 批、每批最多 16 个精确测试名，固定以 NullRHI 分批运行；与顶层 `tests` 或其他批次不得重复。原有 `tests` 保持 1 至 16 项且包含全部计划核心测试。每批必须证明请求与实际发现、完成的测试集合及数量一致，普通断言失败会继续收集，最终仍返回失败。超时、异常执行或源码变化立即停止。批次仅表示请求范围，不自动证明全量覆盖，完整性仍须按当前注册测试清单核对。

执行结果会镜像到已忽略的 `Saved/Automation/<任务编号>/validation-results.json`，保留 HEAD、逐项结果和外层原始回执路径。Claude 编码会话仅获准只读当前任务的外层验证日志目录，可据此处理失败和补齐报告。

需要重新生成 UHT 并干净构建时，可在请求顶层额外设置 `"clean_build": true`。可信外层在同一最终 HEAD 上先运行固定 UBT `-Rebuild -ForceHeaderGeneration -NoUBTMakefiles`，归档构建日志和回执，再运行定向测试；构建失败仍使检查失败。该字段只接受布尔值，不能提供其他命令、参数或路径。DSH 无须在沙箱内绕行 UE 全局缓存或修改生成物。

`--engine` 可指向 UE 安装根目录或其 `Engine` 子目录，构建工具与编辑器均从同一个实际引擎目录推导。

任务检查回执保存在 `runtime/tasks/<任务编号>/validation/<HEAD前缀>-<唯一ID>/receipt.json`。单任务运行编译和定向验证。里程碑验收在最终提交上重跑这些检查，并通过 `milestone_checks` 运行全量自动回归，然后交给 Codex 核对阶段目标和实际证据。

验收命令参数中的 `{workspace}` 和 `{runtime}` 分别替换成独立副本与运行目录的绝对路径。可配置 `success_patterns`、`failure_patterns`（文字片段列表）：所有成功片段必须出现，任何失败片段出现都会使检查失败。这样可以拒绝“进程退出为零，但没有实际执行指定测试”的结果。命令及日志、退出状态、命中的片段都与受审 HEAD 一起保存。

Codex 使用已安装 CLI 的非交互入口及保存的登录状态。dsh 使用正常的 headless 编码工具与本机账号配置，负责生成 Git 提交。安装版本、命令、账号配置路径由配置决定；doctor 会报告缺失条件。不要将账号凭据写进任务书或提交至 Git。

新流程在运行目录的独立 Git 副本及工作分支执行；不自动合并默认分支。未配置 `publication` 时，沿用逐任务发布方式：`push: false` 默认只在本地提交，`push: true` 在每项任务验收通过后推送工作分支。验收依据是实际提交、差异及检查结果，dsh 的完成文字本身不能通过验收。

## 阶段整体发布

需要完整完成一个阶段后统一发布时，添加以下可选对象。此处 M2 是待完成阶段的配置示例，应按实际项目 Roadmap 填写全部范围及证据要求，不能将一项任务通过当作阶段整体完成：

```json
"publication": {
  "mode": "milestone",
  "milestone_id": "M2",
  "goal": "按项目 Roadmap 完成 M2 基础城市闭环全部范围，补齐物流、食物质量和完整饥饿后果，关闭开放审查，以阶段独立测试和真实运行证据验收；不进入 M3。"
}
```

该对象必须且只能包含 `mode`、`milestone_id`、`goal`。`mode` 仅接受 `milestone`；阶段编号为 1–60 位字母、数字、下划线或连字符，且以字母数字开头；`goal` 必须非空并描述完整阶段的范围与完成条件。不设置该对象时，旧配置及原 `push` 行为保持兼容。

启用后，规划器派工，编码端提交，外层执行独立检查，Codex 审核。单项任务在本地积累。规划器提出阶段完成后，调度器在同一固定 HEAD 上重新运行全部独立检查，再由 Codex 阶段审核核对 `publication.goal` 与真实证据。全部系统检查通过且 Codex 接受后，调度器幂等推送配置的工作分支，核对远端 HEAD 并进入 `DONE`。阶段报告单独记录人工验收与待办，人工待确认可以随系统验证通过的里程碑一起发布。系统验证失败仍按审核结果返工或阻塞。

阶段返工会保存反馈并返回规划，继续发布有限修正任务。同一 HEAD 未产生修正却反复提出阶段完成时，达到 `max_reworks` 的重复提案上限后阻塞；有新任务提交会重置该无进展计数。阶段报告保存在运行目录的 `milestones/<阶段编号>/report.json` 和 `REPORT.md`，包含最终 HEAD、任务清单、独立检查、阶段审查和远端发布回执。

配置阶段发布模式即授权系统验证通过后的远端推送，优先于单任务 `push` 开关。仅本地工作的配置可使用 `push: false` 并省略 `publication`。重启会核对远端受审 HEAD，恢复已完成的发布回执。

## 运行与暂停

```powershell
.\unattended.cmd run --once
.\unattended.cmd run
.\unattended.cmd status
.\unattended.cmd pause
.\unattended.cmd resume
.\unattended.cmd retry
```

没有待完成的有限批次时，`run --once` 只推进一个状态步骤并遵守工作时间，`run` 持续等待工作窗口并串行推进。存在待完成批次时，两者先恢复原批次，规则见下文。`pause` 保存暂停请求；`resume` 解除暂停并保留原任务断点，之后重新运行 `run`。失败或阻塞步骤处理完原因后可用 `retry` 重试，任务编号保持不变。按 Ctrl+C 停止前台程序后，下一次运行从持久化任务和实际 Git 状态恢复。

不要同时手工修改运行目录的独立工作副本或启动另一实例；调度器会检测重复进程和意外提交变化，保留现场供核对。中断后不会直接认定编码成功，而是核对原任务、提交和日志后继续。

### 有限任务批次

用 `--tasks N` 限定本批通过验收的任务数量；只执行到该数量，或项目完成、阻塞、暂停等停止条件。`N` 必须为正整数，不能与 `--once` 同用：

```powershell
.\unattended.cmd run --tasks 3
# 仅在本次已明确允许工作窗口外运行时使用：
.\unattended.cmd run --tasks 3 --ignore-window
```

不加 `--ignore-window` 时仍遵守原工作日和节假日规则。`--ignore-window` 必须配合 `--tasks`，只在本批内临时忽略运行窗口，不改真实配置或日历，不注册新的后台任务。批次结束后，再运行普通 `run` 或既有后台入口仍按原时间规则运行。

批次进度保存在运行目录的 `burst.json`。中断后使用相同 `--tasks` 和 `--ignore-window` 参数继续，已验收任务计入原批次限额，不重新派发；批次已经完成时，相同命令只返回该批次结果，不开始另一批。需要另一批时，先在执行器停止后将已完成的 `burst.json` 归档为其他文件名，保留 `state.json` 和原任务记录，再启动新的批次。原批次未完成时不要归档批次文件来重置限额。

普通 `run`、`background start` 和已安装的登录启动入口也会先检查未完成批次，在同一项目与运行目录锁内恢复原任务总数、原窗口覆盖选择和剩余额度，不重新计数。原批次达到目标后，普通持续运行入口恢复真实配置中的时间表；此前的临时全天日历不会用于后续任务。窗口尚未开放、日历年份待更新或已有暂停标记时，持续运行入口会保持等待与心跳，条件解除后续跑同一批次，不会自行 `resume`、清除 `PAUSE` 或转入不受批次限额约束的循环。阻塞或不可恢复失败仍保留断点返回。已完成的 `completed` / `done` 历史记录不会阻止后续正常调度。

普通 `run --once` 存在未完成批次时，只推进该批次一个阶段并保存原目标；如果本步刚好完成最后一项验收，只确认批次结束，不执行下一任务的规划步骤。下次显式普通调用才按原时间表继续。恢复期间沿用原批次已明确授权的 `ignore_window` 选择，不会新增或延长到该批之外。

按 Ctrl+C 中断会保存断点并返回退出码 130；失败或阻塞返回非零退出码。批次数量限制不改变项目阶段发布政策，不能把三项任务通过当作整个 M2 或其他阶段完成。

## 工作时间与节假日

时间固定按北京时间计算，不受 Windows 当前时区影响。默认规则：工作日 `00:00–08:00` 和 `18:00–24:00`；所有周末全天，包括调休周末。`schedule.weekday_start`、`schedule.weekday_end` 可配置；端点采用开始包含、结束不包含。

首次创建配置会复制内置的 2026 年日历到配置目录的 `unattended-holidays.json`；该副本可自行维护，不修改工具内置模板。配置 `schedule.holidays_file` 指向所用日历，相对路径以配置文件目录为基准。下方展示的是格式示例，空列表不代表已核实的全年假期：

```json
{
  "years": [2026],
  "holidays": [],
  "workdays": [],
  "overrides": {
    "2026-11-03": "closed"
  }
}
```

`holidays` 填放假日期，全天可运行；内置日历的 `workdays` 默认留空，按你的要求使全部周末保持全天。若自行希望某个调休周末按工作日时段运行，可将该日期加入 `workdays`。`overrides` 可将指定日期设为 `all_day`、`weekday` 或 `closed`，优先于节假日、工作日覆盖和星期规则。

`years` 表示日历已完整维护的年份。日历没有覆盖当前年份时 doctor 会报告未就绪，持续运行的调度器进入 `calendar_update_required` 等待，不调用模型。更新日历文件和 `years` 后会自动继续，无需重启；调度器在每个步骤前重新读取日历。不能把一份空日历当作已核实的国家法定假日安排。

08:00 到点默认采用 `close_policy: "strict"`：当前步骤的执行期限限制在窗口结束，停止相关进程，保留已有文件、提交、任务和会话断点，到下一工作窗口自动恢复。若希望当前步骤在窗口外限时收尾，可手工改为 `"finish_step"`；该策略停止启动下一步骤，当前步骤最多运行配置的超时时间。两种策略都不会在休息窗口启动新模型步骤。

## Windows 后台与重启恢复

```powershell
.\unattended.cmd background start
.\unattended.cmd background status
.\unattended.cmd background stop
.\unattended.cmd background install
.\unattended.cmd background uninstall
```

`start` 注册并启动隐藏的 Python 后台程序，不弹出终端；任务计划程序中的注册仍对用户可见。`install` 增加当前用户的登录启动触发器，不立即运行循环；后续 `start` 保留该触发器。任务使用当前用户、普通权限和现有登录令牌，不保存密码、不使用 SYSTEM。电脑重启并登录当前用户后恢复，未登录状态不运行；电脑必须保持开机并有网络。

`stop` 先持久化暂停请求，再停止该配置的后台任务。重新运行前先 `resume`，然后 `background start`。`uninstall` 仅移除已停止的任务注册，运行记录保留。后台日志位于运行目录的 `background.log`、`background.err.log`；`background-health.json` 是进程心跳，与任务是否推进分别核对。

这次实现的离线自动化验证与真实项目运行应分开记录。真实 Codex/dsh 登录、完整编码提交、项目测试和持续跨窗口恢复需要在具体项目目标与可用环境上执行验收，不能由本地假模型测试替代。

纯离线演示可运行 `unattended.cmd demo --output "C:\Temp\codex-dsh-demo"`。输出目录必须尚不存在；演示使用假模型、真实 Git 和检查命令，不访问模型服务或远程仓库。

## 已通过任务的额外环境失败

默认情况下，独立检查仍须全部通过。`task_validation_policy` 是用户控制的精确例外，只处理已明确不属于该任务门槛的 `ZhuGuoCity.M2.PackageIntegration` / `live_coding_active`；它不能由 Claude 的验证请求或审核自由文本授予。原始 UE 测试与外层检查继续保留 `passed: false`，阶段打包和全量回归也继续要求真实通过。

策略必须包含以下全部字段：

```json
{
  "task_id": "TASK-LOCAL-0091",
  "scope_sha256": "完整64位原任务范围SHA256",
  "head_sha": "完整候选提交SHA",
  "receipt_sha256": "本轮独立UE receipt.json原始字节SHA256",
  "deferred_tests": [{
    "name": "ZhuGuoCity.M2.PackageIntegration",
    "failure_kind": "live_coding_active",
    "evidence_path": "workspace/Saved/Evidence/TASK-LOCAL-0066/package-uat.log",
    "evidence_sha256": "本轮UAT原始日志字节SHA256",
    "reason": "已有批准范围内，额外打包环境失败留待阶段验证"
  }]
}
```

这是字段说明，示例哈希不是可执行配置。范围哈希由 `ue_checks.task_validation_scope_sha256(task, config["checks"])` 计算，绑定原任务、验收标准、核心测试、基线、编码提供者及完整独立检查配置。验证器还会核对实际 HEAD、干净工作区、受检源码快照、全部请求测试、原始自动化日志、UAT 日志关联、回执字节及路径，拒绝链接、超时、崩溃、未完成测试或任何其他失败。新 HEAD、新回执或变化的证据不能沿用旧策略。

已处于 `BLOCKED / REVIEW` 且本轮独立审核实际为 `accepted` 时，受控制器锁保护的 `Engine.reconcile_task_validation()` 可核实该策略并排到 `PUBLISH`。该调用不调用模型、不执行发布、不新增返工、不改变会话，也不移除 `PAUSE`。审计先写入 `tasks/<task>/validation-gate-<round>-<policySHA>.json`，保存策略、配置/审核/原始证据字节哈希、原返工数与 `deferred_nongating` 状态，再持久化状态；重复执行和审计写完后的崩溃恢复均复用同份记录。后续真正发布前再次验证全部绑定。配置中的这一可选策略不改变旧状态 binding，但审计与发布必须保持其原始配置字节一致。

离线回归：`python -m unittest tests.test_unattended_task_validation_policy tests.test_unattended_ue_checks tests.test_unattended_engine tests.test_unattended_config`。测试使用临时 Git 和假 UE，不调用模型或修改实际项目。
