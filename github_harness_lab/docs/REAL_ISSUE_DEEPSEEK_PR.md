# 真实 Issue → DeepSeek → PR 验证记录

2026-10-28（Asia/Shanghai）。本轮用户授权继续接通本地 Runner 和官方 DeepSeek Harness，并明确允许模型用量及本次 Runner 注册、临时 PR 权限调整。

**固定算术任务的真实链路已跑通。** Issue #3 → GitHub Actions → 本机 Windows Runner → 官方 DeepSeek Harness → 真实本地测试 → 独立云端测试/发布 → PR #4。PR 由 github-actions[bot] 自动创建，代码来自真实 DeepSeek 输出，不是离线假模型。

## 远程证据

| 项目 | 结果与链接 |
| --- | --- |
| 任务 | [Issue #3](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/3)，现为 `agent:review` |
| 真实执行与发布 | [Actions 40633877266](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40633877266)，generate / publish 均成功 |
| 模型生成的 PR | [PR #4](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/4)，open、未合并 |
| 提交绑定检查 | [Review](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/4#pullrequestreview-5677901466) |
| Issue 机器人回执 | [发布回执](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/3#issuecomment-6460870109) |
| PR CI | [Actions 40633978533](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40633978533)，8 项既有回归测试通过 |

- Base：`5d7cc93270c26800bae1c6f309c6039e50ff1d77`
- Result：`54b72884fd94301bc5a1afb78060e942cc831c55`
- PR CI merge preview：`bc8c72db3745ed05df97d87bb36a3cba822fdbc7`
- DSH：官方 Desktop CLI `0.2.1-rc.3`，模型 `deepseek-flash`。
- Session：`session-496196f8-4417-4809-af24-3a3de738af30`。
- Harness 进程退出码 0，`turn_end.reason.kind=completed`，1 次模型请求，0 工具，未超时。
- 源码 SHA-256：`09c249ffa31e6ef0cbca6aa230598985728adc3af72c7598b19e9e03dab61ab9`。
- 本地外层测试：7 项通过；独立云端测试：10 项通过，覆盖新 multiply；PR CI 的 8 项是额外既有回归覆盖，三者分别记录。
- GitHub artifact：`bounded-dsh-3-40633877266-1`，ID `12792447350`，包含脱敏 result.json，按 7 天保留，预计 2026-11-04 UTC 到期。本地副本已保留。

## 调试过程与实际用量

1. [40633288912](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40633288912)：Runner 已接任务，但 Windows PowerShell 执行策略阻止脚本运行，未调用模型。改为 `shell: python {0}`，没有放宽系统策略。
2. [40633536172](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40633536172)：DeepSeek 返回正确函数，guard 记录 117 输入 + 23 输出 token，但官方 provider 向 stdout 写了一行非 JSON 模型目录日志，严格解析器拒绝。原始日志保留在 `.runtime/smoke-output/3-attempt-1-parser-failure/`；当时没有单独保留进程退出码，不能将这次判为完整通过。
3. [40633877266](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40633877266)：仅识别官方目录日志的精确格式进行规范化，其他未知非 JSON 继续拒绝；先持久化进程退出码，再验证协议。完整链路通过。

整个调试共进行了 **2 次真实模型调用，每次 117 输入 + 23 输出，合计 280 token**。每个进程最多一个模型请求、512 输出 token、120 秒。没有自动模型重试；第二次是修复后明确进行的受控重测。未读取账单，不声称实际扣费金额或免费。

## 执行和权限边界

本机使用标准 Windows 用户，Runner 官方版本 `2.338.2` 已通过官方 SHA-256 校验，标签 `dsh-smoke-20261028`，`--ephemeral --no-default-labels --disableupdate`。工作流固定仓库、Issue #3、作者/标签操作者以及标签 `agent:ready`，不执行 PR 提供的控制脚本。

模型使用干净 DSH profile，经官方公开插件接口强制零工具、单请求。现有桌面凭据只由官方 provider 读取，没有复制到仓库或模型工作区。控制器只接受两个小型算术函数的 AST 白名单，禁止 imports、函数调用、属性访问、循环、装饰器和任意模块语句。测试通过后，GitHub-hosted 发布器重新核验 Issue、base 和源码摘要，再创建任务分支/PR。写权限不传给模型进程。

GitHub 对 GITHUB_TOKEN 创建的 PR 将 CI 置为等待批准；当前 Codex 会话检查 diff 后批准了这一次运行。因此本轮有明确的人机监督步骤，**不是 ChatGPT 网页 Scheduled Task 自动验收，也不是完全无人值守**。该行为与 [GitHub 官方令牌文档](https://docs.github.com/en/actions/concepts/security/github_token) 一致。

结束时已验证：Runner 进程退出，`.runner`、`.credentials`、`.credentials_rsaparams` 均不存在，没有安装 Windows 服务；临时开启的“Allow GitHub Actions to create and approve pull requests”已恢复关闭，默认令牌权限保持只读；Issue 从 ready 转为 review，PR 未合并。安装包和脱敏诊断记录保留本地。

## 新增文件与复现

| 文件 | 用途 |
| --- | --- |
| `lab_harness/bounded_dsh.py`、`live_tools/one_request_guard.mjs` | 官方 Headless 单请求适配、零工具策略、进程与流式证据 |
| `lab_harness/live_smoke.py`、`smoke_policy.py` | Issue 来源/内容/base 校验、持久领取、防重复调用、代码白名单和真实测试 |
| `live_tools/deepseek-smoke.yml` | 本地生成与云端发布工作流；远端已部署为 `.github/workflows/deepseek-smoke.yml` |
| `live_tools/publish_smoke.py` | 独立云端测试、任务分支/PR 创建、未知写入对账 |
| `scripts/Prepare-LiveRunner.ps1`、`Connect-LiveRunner.ps1` | 固定版本下载校验、隐藏令牌注册、一次性启动；默认仅 Plan |
| `tests/test_bounded_dsh.py`、`test_live_smoke.py`、`test_smoke_publisher.py` | 本轮 44 项有针对性的离线测试通过 |

关键本地证据：

- `.runtime/live/real-flow-results.json`：本轮最终机器汇总。
- `.runtime/smoke-output/3/result.json`：已上传的脱敏执行证据。
- `.runtime/smoke-output/3/dsh/process-result.json`：实际退出码和超时状态。
- `.runtime/smoke-output/3/dsh/guard-report.json`：请求数、工具数和 usage。
- `.runtime/smoke-output/3/dsh/stdout.log` / `normalized-events.jsonl`：原始/规范化官方输出。
- `.runtime/smoke-output/3/tests/`：实际外层测试日志。

本地验证可运行：

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -p test_bounded_dsh.py -v
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*smoke*.py' -v
.\scripts\Connect-LiveRunner.ps1
```

最后一条只展示 Plan。再次真实运行需新建明确任务、更新受信配置与工作流的固定 Issue/base/body SHA、重新核实授权并注册一次性 Runner；当前 Issue 的已完成 claim 不应删除来盲目重跑。本次没有留后台运行或周期任务。

仍未完成：通用可执行代码 Agent 的 OS/网络隔离、长期 Runner 服务与故障恢复、自动返工以及 ChatGPT 网页两个 Scheduled Tasks 的首次配置和真实验收。这些不影响本次受限链路通过，也不能由本次测试替代。
