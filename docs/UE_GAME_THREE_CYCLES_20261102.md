# ue_game 追加三项真实任务闭环记录

2026-11-02，北京时间。**本批 3/3 项新任务全部通过：TASK-LOCAL-0002、TASK-LOCAL-0003、TASK-LOCAL-0004。** 批次在 16:59:53 达到累计 accepted=4 并停在 PLAN、无活动任务；16:59:54 完成后台恢复，17:01:04 的最终现场记录再次核实后台健康并按原时间等待 18:00。整个 M2 尚未完成，未推送 GitHub，HUD 闪动仍 open。

## 执行范围与入口

- 项目来源：`C:/Users/wy331/OneDrive/文档/ue_game`，远端 `WangYu0611/zhuguo-city`。
- 实际开发副本：`C:/codex-dsh-work/zhuguo-city/workspace`，分支 `unattended/0e175a49c9ee`。源码、任务提交与外层 UE 检查均绑定这一独立 ASCII 路径副本。
- Codex CLI 非交互运行负责有限派工和固定 SHA 独立验收；真实 dsh headless 会话负责编码、按反馈续接返工、报告与任务号 commit。没有通过桌面聊天窗口模拟点击传话。
- UE 构建、定向自动化和必要正常 RHI 取证由可信外层 CHECK 执行并保存 receipt、原始日志与产物，避免要求 dsh 沙箱写入全局 UE 缓存。独立验收读取真实差异及这些证据，DSH 自验声明不直接等于 accepted。
- [首轮 TASK-LOCAL-0001](UE_GAME_MANUAL_RUN_20261102.md) 已在本批开始前通过。本批从累计 accepted=1 开始，要求 **3 项新任务**，固定目标为累计 accepted=4；同一任务内的返工不增加批次完成数量。
- 每项仅本地 commit。所有已通过项的 publication 均为 `local_only: true`、`deferred_to_milestone: M2`。整个 M2 独立检查和 Codex 阶段验收通过后才集中 push，不自动合并 main，不进入 M3。

## 三项最终结果

| 新任务 | 实际范围 | Base SHA | accepted HEAD | 本批状态 |
|---|---|---|---|---|
| TASK-LOCAL-0002 | 显式工具切组后的滚动归属与恢复 | `7ae1fba0260c84617a2321194621ecfeed32eeae` | `1c71273652ecb38114a5cdb93e38e1433b5ed9ca` | accepted；同任务返工 2 次 |
| TASK-LOCAL-0003 | 配送按行进方向检查剩余路段 | `1c71273652ecb38114a5cdb93e38e1433b5ed9ca` | `0e6c6e2f1232a391d124bb0f0e6b5eb985ee7445` | accepted；同任务返工 2 次 |
| TASK-LOCAL-0004 | 持续缺粮触发迁出与足额恢复 | `0e6c6e2f1232a391d124bb0f0e6b5eb985ee7445` | `6fcc7107af32d7855c42ec67b569a8e7c1b5df11` | accepted；同任务返工 1 次 |

上述结论来自各任务目录的 `accepted.json`、绑定同一完整 HEAD 的最终 review 及 `completed-task.json`：0002、0003 最终为 `review-2.json`，0004 为 `review-1.json`。DSH 报告交付时保留的“待外层验证”文字属于其当时执行边界，最终状态由随后 CHECK 和 Codex 独立验收记录补齐，不能将历史待验证文字误读成最终未运行。

### TASK-LOCAL-0002：滚动归属与恢复

修正显式工具同步分类绕过目录切组管理后，来源组位置被目标组写入、目标组位置未按自身记忆恢复的问题。实际验证覆盖 Controller→HUD→Panel 路径、真实分组按钮、实际布局后位置、搜索裁限及真实 HUD Tick 同步。

最终 `review-2.json` 记录：农业位置 80；工具切至生产采集后恢复其独立记忆 160；在生产采集改为 210 后，按钮切至仓储市场恢复 120；返回农业仍为 80，返回生产采集仍为 210。搜索缩短后 offset/end 均为 0。重复工具选择、Tick 和稳定刷新不重复恢复或重建，控件实例与正常动态费用刷新保持。

返工按各自固定 HEAD 归属：

1. `review-0.json`，HEAD `7a22d6bd579c31f32f89a281a4cc65cf504de286`：构建和定向回归通过；正常 D3D12 取证在显式工具选择后对隐藏目录截图，触发 Slate `bWasFound` 断言，进程退出 3，没有五张原图。该轮还缺少实际 HUD Tick 同步分支证明。返工保留产品切至 Context 的行为，在测试宿主恢复目录可见并等待布局，补真实 Tick 覆盖。
2. `review-1.json`，HEAD `09892b9425da6207e3a80befb18f3b2700fd99bb`：构建及相关测试原始结果成功，但新增 Tick 测试名称包含旧 Scroll 测试全名，一个请求匹配两项。严格验证桥因数量不是 1 提前停止，正常 RHI 为 NOT_RUN；这不是再次截图崩溃。返工将名称改为独立的 `ZhuGuoCity.M2.CatalogToolTickSync`，保留断言和单项数量要求。
3. `review-2.json`，HEAD `1c71273652ecb38114a5cdb93e38e1433b5ed9ca`：构建、滚动、Tick、分类、控件刷新、布局预算及正常 RHI 视觉测试通过；46 个输入哈希及 receipt 前后 HEAD 一致。独立验收记录已逐张查看五阶段原图并核对阶段日志和归档哈希。

正常 RHI 入口是 `ZhuGuoCity.Local.CatalogToolGroupScrollVisual`，证据来自脚本驱动的现有 HUD 编辑器窗口。实体键鼠、打包及包内运行为 NOT_RUN；不宣称 HUD 闪动修复。

最终证据入口：

- `C:/codex-dsh-work/zhuguo-city/tasks/TASK-LOCAL-0002/accepted.json`
- 同任务目录 `review-0.json`、`review-1.json`、`review-2.json`、`completed-task.json`
- `C:/codex-dsh-work/zhuguo-city/tasks/TASK-LOCAL-0002/validation/1c71273652ec-0a1f2c5a381c4dfe98cce244368734d8/receipt.json`
- 独立副本 `Docs/Reports/TASK-LOCAL-0002.md`；正常 RHI 原件位于 `Saved/Evidence/TASK-LOCAL-0002/`，包含五阶段 PNG 和 `visual-stages.log`，具体归属与哈希以最终 receipt 为准。

### TASK-LOCAL-0003：配送剩余路段

这是对历史“整条固定路线完整才推进”的**有限物流行为改进**，不将旧契约描述成遗漏缺陷。去程检查 `[NodeIndex, Route.Num()-1]`，返程检查 `[0, NodeIndex]`；当前格、当前有向边另一端和剩余格缺路仍冻结，身后已走过格缺路不再单独冻结。卸粮后原身后缺口进入返程所需区间，下一配送步必须等待修复。停业、缺工、市场入口门控、城市道路隔离及粟豆共用行为保持。

返工按各自固定 HEAD 归属：

1. `review-0.json`，HEAD `b49c9dfcb25e49483722b780eea29490234de982`：构建、定向和正常 D3D12 七张有序画面已有证据，但保存回归读取 Clock 后仅直接推进两支 `World.StepTick`，未证明真实采用加载 Clock 驱动续行。要求两支分别通过各自 `Clock.Advance` 接收相同后续帧时间并按返回步数推进，比较时钟、摘要、进度和粮账。
2. `review-1.json`，HEAD `a3faf3abc377b4c174d7b17315acb4806136f20e`：构建和路线定向成功；保存夹具设置 `AccumulatorSeconds=0.25` 后未重算派生 `BacklogSteps`，连续分支为 0、加载后重算为 2，初始时钟一致性断言真实失败。验证桥提前停止，本 HEAD 后续市场回归及正常 RHI 为 NOT_RUN；旧截图不能转记为本轮成功。返工只同步夹具派生积压，保留双 Clock 推进与所有一致性断言，不修改存档契约。
3. `review-2.json`，HEAD `0e6c6e2f1232a391d124bb0f0e6b5eb985ee7445`：构建、配置基础定向及任务桥六项均通过，完整 HEAD 和 46 个输入哈希一致。真文件加载后采用 World/Clock，双 Advance 分支续行 60 步，摘要、配送、货物与逐资源粮账一致。独立验收记录逐张核对了该 HEAD 重新生成的正常 D3D12 PIE 七张有序帧及阶段日志。

任务桥包含 `DeliveryDirectionalRoute`、`DeliveryDirectionalSave`、`MarketService`、`MarketSave`、`MarketHud` 和正常 RHI `ZhuGuoCity.Local.DeliveryDirectionVisual`。正常 RHI 是脚本驱动的真实 PIE 游戏视口，不能称为实体输入或打包运行。完整物流、食物质量、完整饥饿后果与 M2 最终阶段验收仍未完成，HUD 闪动保持 open。

最终证据入口：

- `C:/codex-dsh-work/zhuguo-city/tasks/TASK-LOCAL-0003/accepted.json`
- 同任务目录 `review-0.json`、`review-1.json`、`review-2.json`、`completed-task.json`
- `C:/codex-dsh-work/zhuguo-city/tasks/TASK-LOCAL-0003/validation/0e6c6e2f1232-a035461a11d04e3c850c56a78320f4be/receipt.json`
- 独立副本 `Docs/Reports/TASK-LOCAL-0003.md`；正常 RHI 原件位于 `Saved/Evidence/TASK-LOCAL-0003/`，含七张有序帧及 `delivery-direction-stages.log`，具体归属与哈希以最终 receipt 为准。

### TASK-LOCAL-0004：持续缺粮迁出与足额恢复

任务书：`C:/codex-dsh-work/zhuguo-city/tasks/TASK-LOCAL-0004/TASK.md`。Base SHA 为 `0e6c6e2f1232a391d124bb0f0e6b5eb985ee7445`；返工实现 SHA 为 `0d23a494f526ac42e32fda1f142a13cdf9f6ff00`；最终受审及 accepted HEAD 为 **`6fcc7107af32d7855c42ec67b569a8e7c1b5df11`**，验收时间为北京时间 16:59:53。报告及 15 个标题仅为 `TASK-LOCAL-0004` 的本地提交齐备，最终工作副本干净。

实际新增规则复用连续真实不足计数及阈值 3：有人住宅持续不足时，即使接路或住满，也按既有间隔和基础批次迁出；方向切换清除旧方向计时，原因改变而方向不变时保留进度；到货未吃、部分消费不解除，真实足额消费后按既定人口/消费 Tick 顺序恢复。人口步骤与住宅查询共用方向和原因判断，HUD 区分持续缺粮、断路及叠加原因。没有新增存档字段或版本，保持 21/dev-20/v21 及旧档保护。

真实规则及劳力回归覆盖第三次不足后下一人口步切向、满屋迁出、准确到期扣人、人口 4→2 和劳力 2→1、原生产批次和配送槽缺工冻结/恢复、自然迁入恢复劳力、空屋保货及资源守恒。三个合法存档边界执行真文件往返，实际采用读回 World/Clock，以相同帧时间推进并逐步比较。

唯一一轮返工归属 `review-0.json`，HEAD `a537e80cef5a35e331ed89ee4c545eb63609a8b2`：构建、Rules 和 Labor 通过，`FamineEmigrationSave` 因整帧推进越过目标步数失败，目标 120/70 实际推进 122/74，计时 20/7 的断言不成立。验证桥提前停止，其他 13 项和正常 RHI 尚未执行。返工保留真实 World/Clock、逐步比较及原断言，改为精确固定步进，并加强自然三次不足与账本证明，不删除失败断言或丢弃 Clock 已消耗的步骤。

最终 `review-1.json` 和 receipt 确认：UE 5.8.4 编辑器构建 PASS；任务请求 **16/16**（15 项 NullRHI、1 项正常 RHI）实际通过；额外 CatalogGroups、HudControlsRefresh、LayoutBudget **3/3** 通过。每项有完整名称、执行数量、原始 Success 和退出码 0；执行前后 HEAD 与输入摘要一致。120/70 步及计时 20/7 的断言保留并实际通过。

正常 D3D12 PIE 入口 `ZhuGuoCity.Local.FamineEmigrationVisual` 由正式建造、道路、市场和消费逻辑生成四阶段证据：

| 原图 | 实际阶段 | SHA-256 |
|---|---|---|
| `01-shortage-moveout.png` | 第三次真实不足后切迁出，显示持续缺粮原因与恢复条件 | `9c106c96a1c7b6824e5e3e8c83ee527ec2c719b9f65e94274d6a0635f05c94d3` |
| `02-emigration-progress.png` | 自然迁出进度 | `8f039b20311dd58bc5cf8fa3f9e6ce1531b1314d1d73c5215c41074cf0559088` |
| `03-arrival-not-recovered.png` | 真实配送到货但尚未吃到，历史仍不足，保持迁出 | `bb1107e33ff444db01a8e8302397ad170322a28db99db8881b56f142a8c75e5a` |
| `04-sufficient-recovered.png` | 真实足额消费后下一人口步恢复迁入 | `775505c757af66717c388c7072a11d97a7d752d02f84f0d7f1206af57cdcd4dd` |

`famine-emigration-stages.log` 的 SHA-256 为 `2e9c0b788a4a3ddb57626a3dc6858795e1e201c4fdeef9fdf3741c72aff0d080`，另记录后续自然迁入与资源守恒。Codex 最终独立验收已逐张查看四张新原图并核对五份归档哈希；验证桥 receipt 的产物/测试记录与这次独立图像内容验收分别归属，不能只凭 receipt 的 passed 推断图像内容已审查。

最终证据入口：

- `C:/codex-dsh-work/zhuguo-city/tasks/TASK-LOCAL-0004/accepted.json`
- 同目录 `review-0.json`、`review-1.json`、`completed-task.json`
- `C:/codex-dsh-work/zhuguo-city/tasks/TASK-LOCAL-0004/validation/6fcc7107af32-e434002ddc904ae0b89578357f766a86/receipt.json`
- 独立副本 `Docs/Reports/TASK-LOCAL-0004.md`；四张原图与阶段日志位于 `Saved/Evidence/TASK-LOCAL-0004/`。

这是新增有限饥饿后果的本任务验收，不代表完整饥荒、整个 M2 或下一阶段完成。正常渲染为脚本驱动真实 PIE；实体键鼠、打包及包内运行仍为 NOT_RUN，HUD 闪动继续 open。

## 批次计数、时间和最终恢复核验

- 批次标识：`b25ed4d360434416b46928ff137b1732`。
- 目标固定为 `start_accepted_count=1`、`requested_tasks=3`、`target_accepted_count=4`。最终 `status=completed`、`completed_tasks=3`、`remaining_tasks=0`、`current_accepted_count=4`，新增清单恰为 0002、0003、0004。达到目标时为 PLAN 且 task=null，本批没有继续派发第五个累计任务。
- 真实工作时间和日历文件未改：北京时间工作日 18:00 至次日 08:00，周末和节假日全天，原截止策略 `strict` 保留。经用户授权的手动越窗使用 `bursts/<批次编号>/calendar.json` 私有覆盖，仅修改批次内部配置。
- 批次完成后恢复流程已核对原 schedule/close_policy 并重新启动原后台。`restoration.json` 完成时间 `2026-11-02T08:59:54.619846+00:00`，即北京时间 16:59:54；回执记录 PID 4476、process_alive=true、healthy=true、任务 state=4、logon_trigger=true。未来工作窗口中的普通后台开发不属于本次已结束的三项限额。

实时状态入口为 `C:/codex-dsh-work/zhuguo-city/burst.json` 和 `state.json`。本批目录是 `C:/codex-dsh-work/zhuguo-city/burst-20261102T065511Z/`。目录中的 `result.json`、`restoration.json` 已更新为本次最终完成结果，和持久化批次编号、目标、accepted 清单一致。初次暂态失败退出属于此前历史，不能与这次完成回执混用。

除恢复回执外，主代理在恢复后再次独立执行现场核验，并于**北京时间 17:01:04** 保存[最终现场核验 postflight.json](<C:/codex-dsh-work/zhuguo-city/burst-20261102T065511Z/postflight.json>)。该文件 `passed=true`，11 项检查全部为 true，包含原日历字节未变、实际心跳等待原窗口、没有 0005、干净受审 HEAD 和后台/登录触发健康。记录内容如下：

- `background status`：running，task state=4（Running），logon_trigger=true，process_alive=true，healthy=true；PID 4476，最终记录心跳年龄约 9.80 秒。
- 项目状态：PLAN、task=null、accepted=4、failures=0、paused=false；sequence=5 表示下一可用编号，本批尚未派发 0005。
- 原日历覆盖有效，当前 `allowed=false`、`next_open=2026-11-02T18:00:00+08:00`；后台恢复后按原工作时间等待，没有继续使用私有越窗日历。
- 独立副本工作树干净，分支仍为 `unattended/0e175a49c9ee`。原 `ue_game` HEAD 仍为 `bfd142b1353868c882990155ab555c33ac11199d`，原有未跟踪历史日志保留。
- 累计四项 accepted 均为 local_only、deferred_to_milestone=M2；未提前推送、合并 main 或进入 M3。

以上同时核对了实际进程、Windows 任务和新鲜心跳，不将 `started` 或 `original_schedule_restored=true` 单独作为后台健康证据。

## 框架恢复回归的归属

本轮框架加入普通 `run`、单步及登录后台对未完成有限批次的锁内恢复：沿原固定目标继续余额，不重入项目锁、不重置计数、不清除显式 PAUSE。普通持续运行遇到窗口关闭、日历待更新或暂停时保持心跳并等待；批次完成后回到未改动的原工作日历。显式有限批次和单步入口仍可立即返回等待状态。

实现代理 `cli_delivery` 已执行验证：**恢复专项 10 项通过，相应关联回归运行 59 项通过**。这是指定专项和关联范围的执行记录，本页没有重新运行测试；不得将 10 与 59 相加，也不与早期 63 项定向或其他历史数字相加，冒称最新完整测试集总数。

## 范围与未关闭项

本次三项有限任务已全部 accepted，并验证累计上限及恢复原后台。完整 M2 的进一步物流、食物质量、完整饥饿后果与阶段最终复核仍需按当前路线继续；本记录不宣称整个 M2 已完成、GitHub 已推送或 HUD 闪动已修复。所有正常 RHI 证据保留各自宿主和脚本驱动边界，不扩展成实体输入或包内运行通过。
