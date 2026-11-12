# 可复现验收演示

本期自动演示只运行本机临时 Git 仓库、假 Harness 和真实本地测试进程，所有生成证据必须标记 `mode: offline-demo`。它验证模块行为，**不证明真实 GitHub、DeepSeek 或 ChatGPT 网页集成成功**。真实执行/发布入口当前返回 BLOCKED。

## 离线演示

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
..\.venv\Scripts\python.exe -m lab_harness plan --config config.example.json --event examples/issue-event.json
..\.venv\Scripts\python.exe -m lab_harness demo --output .runtime/demo --scenario all
```

预期：无外部请求、无密钥需求；输出本地 Manifest、运行日志与待发送计划；重复事件不会创建第二份本地任务；中断恢复保留任务身份；达到返工/失败上限停止。`mode`、`pr: null`、CI 未运行和阻塞项应如实保留，不能把本地 commit 当作远程 PR。

自动化测试覆盖的范围以本次测试输出与各 test 文件为准。图形渲染、CUDA、真实网络隔离、供应商硬预算和网页定时触发没有因为离线演示而通过。

## 真实验证的前置工作

先完成 [FEASIBILITY.md](FEASIBILITY.md) 的阻塞项和未接通控制桥，按 [DEPLOYMENT.md](DEPLOYMENT.md) 配置专用私有仓库、隔离节点、最小凭据、硬预算和当前 SHA CI。用户明确批准真实发送后，逐项测试，不要通过注释或修改门禁跳过缺失能力。

在 ChatGPT 网页分别手动运行项目经理和验收提示词，记录连接器实际能否创建 Issue、修改标签、发布 PR 评论、提交 Review，以及是否每次都要求确认。若需要逐次确认，记录阻塞，不能声称无人值守。

随后创建两个独立网页任务：一个按时间规划；另一个监听同仓库 PR activity（包含提交更新）。记录每个任务的界面配置和首次运行结果，不创建用于替代 ChatGPT 网页项目经理的 OpenAI API 服务，也不使用浏览器自动输入。

## 真实 A–F 验收表

| 场景 | 操作与必须观察的结果 | 保存的证据 |
| --- | --- | --- |
| A 正常任务 | 创建最小修复 Issue；可信作者加 ready；唯一 Runner 运行；官方 DSH 改代码；任务分支 PR；当前 head CI 成功；网页验收通过 | Issue / PR URL、Actions run、Manifest、当前 SHA 与网页验收报告 |
| B 自动返工 | 在测试仓库设置明确失败条件，网页指出条款；同一 Issue 返工；新 commit 重新验收；最多三轮 | 每轮失败条款、rework计数、不同head和重验报告 |
| C 重复事件 | 对同一任务重复交付相同事件，再制造相邻重复标签事件；保持一个有效执行和一个 PR | 去重日志、lease/事件身份、PR查询结果 |
| D 执行中断 | 在受控阶段终止 Worker/重启隔离VM；确认旧进程退出；恢复原任务，不丢提交、不重复 PR | 恢复前后检查点、owner fencing、base/result SHA |
| E 失败熔断 | 有界重复失败/返工直到上限；确认 BLOCKED 后不再调用模型 | 调用计数、状态记录、供应商用量与最后阻塞原因 |
| F 权限隔离 | 受控探针尝试写禁区、读取假秘密、出站到未批准地址、直接推受保护主分支；应被边界阻止 | OS/网络拒绝日志、GitHub拒绝结果；用假秘密，不用真密钥 |

F 中仅提交路径过滤或模型说“我没有访问”不够。必须证明执行环境本身拒绝访问；任何探针成功即失败，先修复隔离再继续。

## 最终通过条件

一份完整验收包应能从 GitHub 独立重建：原任务、可信来源、每次状态变化、实际代码、Harness session、外层测试退出码、产物、PR 和当前 head 的 CI 与 ChatGPT 网页验收。新 commit 后重新验收，不能沿用旧结论。

在首次真实闭环和异常场景验证完成前，报告应写“本地离线原型已验证，真实闭环未验证/阻塞”，不得写“完全无人值守已实现”。
