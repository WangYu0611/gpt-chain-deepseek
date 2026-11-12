# 两次自动返工的 Runner / Actions 实施方案

审查日期：2026-10-28（Asia/Shanghai）。本文是实施设计，**不是两轮真实运行成功报告**。仅审查既有代码和 GitHub 官方文档，没有注册 Runner、生成凭据、调用模型或修改远程仓库。

验收语义采用更严格定义：同一个 Issue、同一个 PR，初始 head H0 被真实 ChatGPT 网页任务判为 FAIL，自动生成 H1；H1 再被判为 FAIL，自动生成 H2；H2 被判为 PASS。共有三次候选生成和两次自动返工。人工发送的 Review、脚本自写的 ChatGPT 标签和离线模拟不计入网页任务验收。

## 既有代码为何不能直接循环

| 位置 | 当前行为 | 所需变化 |
| --- | --- | --- |
| `live_tools/deepseek-smoke.yml` | 仅 Issue #3 的 owner `agent:ready` 标签事件，单次 generate/publish | 新建独立有界循环 workflow；保留既有单次证据 |
| `lab_harness/live_smoke.py` | 单 Issue 全生命周期一个排他 claim；固定只读 main 的初始源 | 按 `(loop_id, round, parent_head, accepted_review_id)` 排他领取；返工读取上一 head 和真实验收反馈 |
| `lab_harness/smoke_policy.py` | 固定 add/multiply；结果必须全部本地测试成功 | 新协议区分安全校验、执行完成和产品验收；允许安全候选携带真实失败测试结果进入验收，不能把失败伪装为成功 |
| `live_tools/publish_smoke.py` | 一个 commit 必须直接以 base 为 parent；已有 PR marker 必须等于这一次 source | 同一 PR 上非强推增量提交，parent 必须是上一次已验收 head；幂等标识按 loop/round/source；未知结果先对账 |
| `scripts/Connect-LiveRunner.ps1` | `--ephemeral` 一个 job 后注销 | 三个独立一次性 Runner，分别服务三个生成 job；或另行审查有总时限的多 job Runner。不能把一次性 Runner 当作可连续消费三个 job |
| `lab_harness/bounded_dsh.py` | 每次一个官方请求、无工具、独立 fresh profile | 每轮各一次请求与独立目录；继续版本、模型、输出、时长上限；更新授权记录的适用范围，不复用旧 claim 或旧输出 |

本次不能通过仅删除旧 claim、重贴标签、伪造 Review 或把一个模型内部 turn 算作一个完整闭环来达成目标。

## 推荐的最小受控执行拓扑

一个受信任 workflow 的固定 DAG：

```text
generate-0(local ephemeral) -> publish-and-check-0(cloud)
 -> await-web-review-0(cloud, bounded polling)
 -> generate-1(local ephemeral) -> publish-and-check-1(cloud)
 -> await-web-review-1(cloud, bounded polling)
 -> generate-2(local ephemeral) -> publish-and-check-2(cloud)
 -> await-web-review-2(cloud) -> final-proof(cloud)
```

- 三个本地 Runner 各有独立注册目录和一次性名字/标签；只运行预装可信 Python 控制器，不 checkout、不运行 PR 脚本。相同可信控制器配置固定仓库、Issue、loop_id、workflow SHA、过期时间、最大轮次和模型请求总数。注册后不安装服务，启动进程窗口隐藏；总截止到达即停止所有未消费 listener，并核查注册状态。
- 每个生成 job 只给 GitHub 读权限。每个模型子进程继续使用过滤环境，GitHub token 不进入模型环境；DSH 官方账号读取路径保持不复制。每轮 fresh DSH profile，不并行调用模型。
- 每轮云发布器只 checkout 固定可信 workflow 版本；下载精确 artifact ID，核对 producing run/attempt/round/head。重读 Issue、PR、最新 head 后才提交；候选代码只在既有算术 AST 白名单范围执行。安全校验失败立即 BLOCKED。
- 每轮发布器将真实独立测试结果写到 **该候选 head SHA** 的 commit status，例如 `harness/independent-tests`，target_url 指向精确 run/job。写 status 需要 `statuses: write`。状态不能使用工作流 base SHA，也不能仅依赖整个 orchestration run 的最终绿色结果。
- 云端等待器读取 GitHub 真正的评论/Review，不接触 ChatGPT 私有接口、不模拟输入、不替 ChatGPT 写结论。绑定 repository、Issue、PR、loop_id、round、head、review ID、可信作者、验收来源以及自动任务运行凭据。网页 task ID/run URL 必须能独立核实；评论正文自称“ChatGPT”或作者恰好是 owner 均不足以证明自动来源。
- 每个等待器有硬超时（建议每轮 20 分钟，并设置整次实验截止）；审批待处理、网页任务没有运行、head 变化、任务关闭、未知写入结果或上限触发时停止并给可恢复状态，不能偷偷补发人工评论。
- 在演示路径中只允许 H0 FAIL -> H1 FAIL -> H2 PASS。若 H0/H1 提前 PASS，诚实记录实际结果；不能为了凑轮数把 PASS 改成 FAIL。通用策略最多 3 次返工，此次可把可执行预算收紧为 2 次返工、3 次模型请求。

三个 Runner 只解决作业供给，不代表 OS 隔离。仍只适用经过 AST 限制的小型算术演示。仓库中其他能指定同标签的受信任 workflow 也具有调度权限，因此要在注册前检查该私有仓库的 workflow 版本，并把注册存在时间限制到此次实验。

## 消除每轮人工批准 CI 的路径

GitHub 当前官方规则明确区分三类行为：使用 GITHUB_TOKEN 产生的多数事件不启动新工作流；`workflow_dispatch` / `repository_dispatch` 可启动；`pull_request` 的 opened/synchronize/reopened 会启动等待人工批准的 CI。GITHUB_TOKEN 写 issue_comment、label、push 不能用来构造递归闭环。其他 GitHub App installation token 或 PAT 可以支持自动 PR 事件工作流。[GitHub 触发规则](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

因此本演示优先采用同一可信 DAG 内的独立检查和 SHA 绑定 status，**不把原 approval-required 的 `pull_request` CI 声称为已通过**。ChatGPT 验收提示词应明确本实验需要的检查 context；如果仓库规则要求原 PR workflow 成功，仍必须解决该要求，不能绕过分支保护。[GITHUB_TOKEN 规则](https://docs.github.com/en/actions/concepts/security/github_token)

另一种可审查方案是发布后通过 `workflow_dispatch` 显式启动 `bounded-candidate-check.yml`，`ref` 指向可信控制代码的 main/tag，候选 head 只作为严格校验的 input。调度 job 需 `actions: write`；目标 workflow 明确支持 dispatch。核验返回 run ID（当前 API 文档为 200 且返回 workflow_run_id/run URLs）；仍按固定 context 把检查绑定到候选 head，不能执行候选提供的 workflow。[Dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)

如果需要原生 PR-event CI 自动启动而非上述受控检查，需单独配置仅此仓库的 GitHub App installation 凭据或最小范围 PAT；这是另一条授权路径，不是本最小实验的前提。不要把 GitHub token 写进 ChatGPT 提示词、Issue 或日志。

Commit status 有独立 context、state、target_url，反映在包含该 SHA 的 PR 中。读取和发布都应以 SHA 为准，并核实 creator、预期 context、目标工作流、轮次记录和实际测试结果；只看 combined status 的文字摘要不足以证明正确来源。[Commit statuses API](https://docs.github.com/en/rest/commits/statuses#create-a-commit-status)

## 如何产生可复现的两次验收失败

预先公开测试设计：优先采用含两个已知独立缺陷的受限算术 fixture，让验收器根据固定计划逐次报告一个 blocking finding，DeepSeek 每轮只修当轮 finding 并保持其余接口。初始生成也应有真实 DSH session；各缺陷、报告顺序和停止条件在 Issue 中提前说明。若实际模型提前修复所有缺陷，记录少于两次返工，重新设计独立演示，不伪造失败。

需要故障注入时，应在 Issue、证据和最终结论中明确写为故障注入演示：记录模型原始输出摘要、注入后候选摘要、注入规则和触发轮次。它证明反馈传递、返工调度和幂等，不证明模型自然产生缺陷，更不能把改写后的代码完整归因于模型。未经说明的定时改坏代码不可用于通过“两轮自动”验收。

当前 `live_smoke` 在本地测试失败就中止，`publish_smoke` 也在独立测试失败时停止。若演示目标包含真实失败测试进入 PR 验收，需要新增 candidate schema/发布路径，保留安全校验门禁，把 `test_failed` 与 `execution_failed` 分开：前者可以形成含失败证据的待验收候选，后者不能成为可接受结果。既有通过 schema 不应被放宽来掩盖错误。

## 实际运行前的一次性配置与授权范围

先把新代码、固定 workflow、Issue 任务卡、ChatGPT 任务提示词和本地配置做完并审查，再申请必要权限：

1. 用户账户中确实存在并可启用的 ChatGPT 网页定时任务，以及可后台读取/写回此私有仓库的连接权限；首次必须核实真实触发记录。当前 desktop automation 不能替代该证据。
2. 仅为该仓库注册并启动三个单 job Runner，实验结束或固定截止时停止/注销未消费实例；不安装服务。GitHub 官方说明 ephemeral runner 消费一个 job 后自动注销。[Runner reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
3. 实验期间临时允许 Actions 创建 PR，云发布器仅此仓库 contents/pull-requests/issues/statuses 写权限；不自动合并，不写 main 的任务结果，结束恢复开关。若用 dispatch 备选路径，另给调度 job actions 写权限。
4. 已有“随便使用”的模型使用授权无需重复询问费用；具体实验配置仍记录最多三个有界生成请求，并明确网页模型使用和运行时限，不宣称货币硬封顶。

验收时必须提供 H0/H1/H2、三个 DSH session、每轮实际测试、两个 FAIL Review 与最终 PASS Review 的 ID/URL、两个自动任务运行来源，以及中途零人工介入证据。未拿到这些事实，只能报告“代码/离线协议验证完成，真实网页闭环尚未验证”。
