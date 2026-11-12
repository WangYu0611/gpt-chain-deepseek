# 本机验证记录

验证日期：2026-10-28（Asia/Shanghai）。本页保留首轮离线验证。最新 [真实 Issue→DeepSeek→PR 记录](REAL_ISSUE_DEEPSEEK_PR.md) 已验证官方模型、本地 Runner 与自动 PR；本地 7 项、云端独立 10 项、PR CI 8 项通过，并有 44 项新增针对性离线测试通过。完整无人值守闭环仍未完成。

## 首轮离线结果

- `scripts/verify_local.py`：**119 项测试通过，0 失败，0 错误，0 跳过**。
- 六类演示 A–F 完成；A/B 使用假 Harness 和假验收者，代码修改、测试进程和本地 Git commit 是真实执行；D 演示是租约检查点模拟，额外 Worker 单元测试模拟提交后硬中断并验证恢复；F 只验证策略，不证明 OS/网络/远程分支保护隔离。
- 6 个 PowerShell 脚本通过 AST 语法检查；4 个部署入口默认 Plan 输出已执行检查，没有下载、安装、注册、启动服务或外发。
- `plan`、默认 `run`、`doctor` 不启动模型进程；`run --execute`、`publish --execute` 在默认配置下都返回 BLOCKED。`publish` 默认仅输出待发送计划。
- JSON Schema 文件的 JSON 语法已检查；完整 JSON Schema validator 和 YAML parser 未安装，完整解析验证均标为 **NOT_RUN**。
- 首轮当时没有真实外发。后续已验证真实 Issue、PR、GitHub CI；DeepSeek、ChatGPT 网页任务验收与真实 A–F 场景仍为 **NOT_RUN**。

## 可检查产物

从试验区根目录查看：

- `.runtime/verification/report.json`：机器可读最终结果及本次演示目录。
- `.runtime/verification/unittest.log`：全部测试名称和真实执行结果。
- `.runtime/verification/additional-checks.json`：PowerShell 语法及发布门禁验证。
- `report.json` 中 `demo_summary` 指向本次六类演示记录；相邻 `runtime/runs/` 目录包含每次运行的 `journal.json`、`evidence.json`、测试 stdout/stderr 与 `outbox.json`。

Manifest 的 `mode` 为 `offline-demo`，`pr` 为 null，CI 为 NOT_RUN，outbox 的 `sent` 为 false。代码不能把这些记录当作真实发布授权或线上验收。

## 复现

在试验区根目录执行：

```powershell
..\.venv\Scripts\python.exe scripts/verify_local.py
```

每次生成新的演示目录；汇总报告覆盖为最新结果，历史演示不自动删除。可选解析库可在经审查的开发环境按 `requirements-dev.txt` 安装后重跑；本次未安装任何包。

尚待完成的工程与权限条件见 [实施状态](IMPLEMENTATION_STATUS.md)，真实验收步骤见 [演示文档](DEMO.md)。
