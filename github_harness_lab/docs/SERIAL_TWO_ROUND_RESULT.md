# GPT × DSH 连续两轮实测结果

**通过。** 2026-10-28，网页 GPT 发布第一项文档任务，DSH 完成并提交报告；网页 GPT 自动发布第二项，DSH 再次完成并报告；网页 GPT 随后自动发布第三项。第三项只用于证明连续派工，`dispatch=false`，没有执行。两份文档 PR 均未合并。

## 实际顺序

以下时间均为北京时间（Asia/Shanghai）。仓库为 `WangYu0611/chatgpt-deepseek-harness-test-20261028`。

| 时间 | 实际结果 | 可核对证据 |
| --- | --- | --- |
| 16:24:05 | 网页 GPT 发布 `TASK_M0_1`：写项目简介 | [Issue #9](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/9) |
| 第一项完成后 | 官方 DSH 写文档，云端发布器检查并提交报告 | [PR #10](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/10)、[Actions 40656516069](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40656516069) |
| 16:26:05 | 独立网页报告任务接收第一项报告，自动发布 `TASK_M0_2`：写三条待办清单 | [Issue #11](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/11)、[第一项验收评论](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/10#issuecomment-6464253595) |
| 第二项完成后 | 官方 DSH 写文档，云端发布器检查并提交报告 | [PR #12](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/12)、[Actions 40656756951](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40656756951) |
| 16:28:34 | 同一独立网页报告任务接收第二项报告，自动发布 `TASK_M0_3`，明确禁止执行 | [Issue #13](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/13)、[第二项验收评论](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/12#issuecomment-6464297738) |

## 执行与网页来源证据

成功复测 nonce 为 `d11729d0-6c10-4199-9066-b78cf1b4f93e`，固定 main 为 `a908bf3957edb91bd842992cf83cab88b3d3b2ef`。控制记录 [Issue #5](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/5) 已以 completed 关闭，[最终远程记录](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/5#issuecomment-6464376142) 汇总结果与清理状态。

| 证据 | TASK_M0_1 | TASK_M0_2 |
| --- | --- | --- |
| 报告 PR | #10 | #12 |
| 精确 head | `5d2a462709e9f2c732be7640c885743c4a7d8764` | `45ba708d4fc4631757763494c7aa713363966225` |
| DSH Session | `964e558d-17d6-425c-a163-c47e4c0905df` | `ea2a6f5c-c9a1-4233-b939-163e51d59ff8` |
| Actions run | `40656516069` | `40656756951` |
| generate job | `122708742771`，成功 | `122709534170`，成功 |
| publish job | `122708933961`，成功 | `122709744661`，成功 |
| DSH 请求 / tokens | 1 / 217 | 1 / 142 |

成功接收报告的是独立网页任务 `6ae1b067d5b48191965a3d6af74eaa2d`，所在网页对话为 [连续派工报告任务](https://chatgpt.com/c/6ae1b031-e608-83ec-9fb2-2b2c1f4f750b)。它实际收到两个 `actor_login=github-actions[bot]`、`actor_is_bot=true` 的 PR `opened` 事件，并完成后继任务创建：

- 第一项报告 → 第二项：网页运行/turn `01a1819c-dd4b-77fd-b350-d5e2534bc234`。
- 第二项报告 → 第三项：网页运行/turn `01a1819e-cdec-72d4-bd52-d3aa1e761668`。

已保存的本地来源记录为 [serial-success-web-provenance.json](../.runtime/serial-success-web-provenance.json)。证据包含实际事件、网页工具调用与运行记录；结论不是依据机器人评论自报来源，也不是依据离线测试推断。成功复测的后继任务由网页报告任务发出，没有以手工补发来代替自动推进。

网页结果截图：[serial-two-rounds-complete.png](../.runtime/live/serial-two-rounds-complete.png)。

## 前一尝试与调用统计

前一尝试 [Issue #7](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/7) / [PR #8](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/8) 已真实完成一项，但报告处理与启动任务共用对话，后者唤醒时打断了连续推进。该尝试已归档，**不计为连续两轮成功**。随后将报告处理移到独立网页任务再测。

| 范围 | DSH 请求 | tokens |
| --- | ---: | ---: |
| 前一未完成连续推进的尝试 | 1 | 207 |
| 成功复测 | 2 | 359 |
| 本次受限测试合计 | 3 | 566 |

此前 `DSH-REWORK-002` 返工实验已撤销，未调用模型。23 项针对性离线测试用于检查本地实现；它们与本次真实运行证据分开，不作为线上成功的替代。

## 已完成清理与结论范围

- 所有网页测试任务已暂停，包括启动任务、旧报告任务和新的独立报告任务。
- 三个一次性 Runner 全部以 code 0 退出，GitHub 服务端显示 `There are no runners configured`。
- Actions 创建/批准 PR 权限已恢复 OFF；本地 serial 执行开关 `enabled=false`。
- 未安装常驻服务；两份成功报告 PR 未合并，第三项没有执行。
- 测试及清理在授权截止时间 2026-10-28 16:58:46（北京时间）前完成。

本次证明的是：**简单文档任务可以连续完成两次自动“报告 → 下一任务”转换。** 长期无人值守、任意编码、自动合并、故障恢复和持续运行可靠性没有在本次测试中得到证明。
