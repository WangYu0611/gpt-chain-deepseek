# 真实 GitHub 联动测试

本页是首轮 GitHub-only 历史记录。后续已完成 [真实 Issue → DeepSeek → PR](REAL_ISSUE_DEEPSEEK_PR.md)，请以该记录了解最新状态。

日期：2026-10-28（Asia/Shanghai）。用户在最初禁止发送之后明确要求：“你自己创建一个github的测试仓库，看看能不能联动起来”。本轮据此创建独立私有仓库并执行小型真实 GitHub 测试。

**结果：GitHub 链路已打通；完整 Issue → DeepSeek → PR → ChatGPT 网页自动验收闭环尚未完成。** 本轮没有模型调用、Runner 注册、网页 Scheduled Task 创建、自动合并或对原项目的远程发布。GitHub 连接器没有创建仓库工具，因此仅建仓使用登录中的 GitHub 网页，后续写入和证据读取通过连接器完成。

## 可核查的远程记录

私有仓库：[WangYu0611/chatgpt-deepseek-harness-test-20261028](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028)。仓库中只包含合成测试样例 `live_fixture/` 的内容，没有上传原项目、配置或凭据。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 创建 Issue 并添加可信触发标签 | PASS | [Issue #1](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/1)，标签 `integration:probe` |
| 标签事件触发 Actions | PASS | [回执运行 40630961802](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40630961802)，`receipt` job 成功 |
| Actions 向 Issue 回写结果 | PASS | [github-actions[bot] 回执](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/issues/1#issuecomment-6460462971)，含唯一标记和真实 run URL |
| 独立分支创建 PR | PASS | [PR #2](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/2)，`agent/issue-1` → `main`，保持 open / unmerged |
| PR 事件触发真实 CI | PASS | [CI 运行 40630999411](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/40630999411)，10 项测试通过 |
| 读取 diff/提交/CI 后发布 Review | PASS | [提交绑定的 COMMENT Review](https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/2#pullrequestreview-5677719525)，已回读确认 |
| DeepSeek 生成和提交代码 | NOT_RUN | 本轮 PR 由当前 Codex 会话创建 |
| Windows self-hosted Runner | NOT_RUN | 本轮运行在 GitHub-hosted Ubuntu |
| ChatGPT 网页 Scheduled Task 自动验收 | NOT_RUN | 本轮 Review 由当前 Codex 会话通过连接器发布 |

GitHub PR CI 实际测试的是合并预览，不是直接 checkout head：

- Base SHA：`3364f2cd14a44ca0c43dd8b4a9de4bece20607e7`
- PR Head SHA：`65a0f5c7b67ef9a8a4a514f3b55220108438e8f7`
- CI Merge SHA：`656c874b1d501d117ad4f681a386cb83fbaa5dce`
- CI Job ID：`122627086563`
- 真实命令：`python3 -m unittest discover -s tests -v`
- 日志：`Ran 10 tests in 0.002s`，`OK`。

PR 只增加 `tests/test_numeric_edges.py` 的两项测试，共 14 行。Review 使用 COMMENT，明确区分当前会话测试与网页定时验收，不构成独立审批，也没有设置 `agent:accepted`。后续 head 变化必须重新验证。

## 本地交付

- `live_fixture/`：已发布的两条最小工作流及样例源码、测试；本地副本包含 PR 中的新增测试文件。
- `lab_harness/dsh_legacy.py`、`tests/test_dsh_legacy.py`：本机官方 0.2.1-rc.3 的独立适配器和 12 项离线测试，已另行只读审查。
- `.runtime/live/github-results.json`：仓库、Issue、PR、Review、CI、SHA、日志摘录和未执行项，格式为 GitHub 探针报告，不冒充完整 Worker Evidence Manifest。
- `.runtime/live/pr-review.md`：已发送的 Review 正文副本。
- `.runtime/live/preflight-dsh.json` 和 [DSH 预检说明](LIVE_DSH_PREFLIGHT.md)：真实 CLI 版本/help 结果及兼容性差异。

运行本地样例测试（从试验区根目录）：

```powershell
Push-Location live_fixture
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -v
Pop-Location
..\.venv\Scripts\python.exe -m unittest discover -s tests -p test_dsh_legacy.py -v
```

这些本地测试不发送消息。首次完整原型的 119 项离线测试记录保留在 [VERIFICATION.md](VERIFICATION.md)。

## 后续真实验证与运行边界

1. 在此测试仓库新建测试 Issue，添加 `integration:probe`，可重现 Issue → Actions 回执；工作流限定仓库、标签及操作者 WangYu0611。该探针只是一次回执测试，没有承诺重复事件幂等。
2. 在独立分支新增确定性测试并打开 PR，可重现 PR → CI。检查实际日志和最新 SHA，不能只看 PR 正文声称通过。
3. 以上操作会再次真实写入 GitHub，并使用少量 GitHub Actions 资源。两个工作流各自最多运行 5 分钟，没有周期调度。该时限不是 GitHub 账户费用硬上限；本轮未修改计费设置。
4. 目前无需停止任何本地后台进程。要暂停此 GitHub 探针，在仓库 Actions 中 Disable 对应工作流；Enable 后恢复。删除标签不会撤销已排队的运行；运行中可使用 Cancel run。
5. 要继续 DeepSeek 真正执行，需验证账号/模型可用性、供应商或网关费用硬上限、独立执行环境与网络隔离，接通受信 Worker 和发布边界。不能用配置布尔值代替这些实测。
6. ChatGPT 网页两个任务仍须按 `prompts/` 和部署文档完成首次配置，核实该账户后台读写能力及逐次审批要求。当前 Codex 连接器写入成功不能证明网页 Scheduled Tasks 同样能后台完成。

本轮没有部署原型 `workflows/` 内的 Worker 调度，也没有修改默认关闭的配置。完整真实 A–F 验收、远程状态恢复、主分支保护和无人值守费用限制仍待完成。
