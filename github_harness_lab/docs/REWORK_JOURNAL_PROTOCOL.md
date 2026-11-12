# 有界返工 journal 协议

这是纯函数协议和离线验证组件，不发送请求、启动模型或创建网页任务。它支持三次独立代码修订：首次验收失败 → 返工 1 再失败 → 返工 2 通过。只有独立核验真实 ChatGPT 网页任务运行记录后，才能把这两轮认定为自动闭环。测试样例和身份字段自述都不是线上证据。

## 接口

```python
from lab_harness.rework_protocol import Policy, encode_record, replay_journal

policy = Policy(
    repository="WangYu0611/chatgpt-deepseek-harness-test-20261028",
    issue=5,
    pr=REAL_PR_NUMBER,
    base_sha=PINNED_BASE_SHA,
    criteria_sha256=PINNED_CRITERIA_SHA256,
    workflow_sha=PINNED_WORKFLOW_SHA,
    publisher_login="github-actions[bot]",
    reviewer_login="WangYu0611",
    automation_id=REAL_REVIEWER_TASK_ID,
    required_criteria=("arithmetic-correctness", "scope-safety", "execution-evidence"),
    max_reworks=2,  # 本次演练限定；通用协议最多允许3次
    journal_location="pr",
    loop_id="DSH-REWORK-002",
)
state = replay_journal(policy, fetched_comments, mode="operational", observed_head_sha=current_head)
```

`fetched_comments` 必须来自已认证 GitHub API 的完整分页结果，按创建顺序排列，字段包含 `id/body/user.login/html_url/created_at/updated_at`。正文使用 `encode_record(record)` 生成：第一行固定为 `<!-- harness-rework-journal:v1 -->`，第二行起是一个完整 JSON 对象，不能添加 Markdown 围栏或解释段落。

默认 journal 位于 PR，实际评论链接须为 `https://github.com/<repository>/pull/<pr>#issuecomment-<id>`；只有显式 `journal_location="issue"` 才接受原 Issue 评论链接。普通讨论被忽略，带协议 marker 的非法记录使重放停止。原始事件禁止编辑；后续修正需要外部对账。

`mode="operational"` 允许已经认证的可信 GitHub 评论作者驱动返工，但报告 `chatgpt_origin_verified=false`、`two_automatic_reworks=NOT_VERIFIED`。它要求当前 PR head，避免沿用旧 head 验收。`mode="offline"` 适合测试，永远不能报告真实自动闭环。

`mode="live"` 是最终证据审计模式，必须传 `verifier(record, observed_comment)` 和当前 head。候选证据核验器返回 `VerifiedEvidence("github", automatic=True)`；验收证据核验器返回 `VerifiedEvidence("chatgpt_web_scheduled_task", automatic=True)`。回调必须独立核验官方来源，不能仅根据正文中的字段返回成功，也不能从 JSON 反序列化这个结果对象。任意人工介入应返回 `automatic=False`，最终自动闭环结论保持 `NOT_VERIFIED`。测试里的模拟回调严禁接入线上。

## 候选事件

下列占位值仅示范结构，不是实际运行记录。替换时保留 JSON 类型：Issue、PR、revision、run_id、run_attempt 和 previous_event_id 是数字；Git SHA 为完整 40 位小写十六进制，SHA256 为 64 位。

```json
{
  "schema": "harness-rework-journal/v1",
  "kind": "candidate",
  "loop_id": "DSH-REWORK-002",
  "repository": "WangYu0611/chatgpt-deepseek-harness-test-20261028",
  "issue": 5,
  "pr": 123,
  "revision": 0,
  "base_sha": "<pinned base 40-hex>",
  "criteria_sha256": "<pinned criteria 64-hex>",
  "head_sha": "<candidate head 40-hex>",
  "parent_sha": "<base SHA for revision 0; previous head thereafter>",
  "previous_event_id": null,
  "execution": {
    "run_id": 123456,
    "run_attempt": 1,
    "stage_key": "generate-0",
    "workflow_sha": "<pinned workflow 40-hex>",
    "session_id": "<real DSH session ID>",
    "source_sha256": "<actual published source 64-hex>",
    "artifact_sha256": "<actual evidence artifact 64-hex>",
    "completed": true,
    "model_requests": 1
  },
  "ci": {
    "run_id": 123456,
    "run_attempt": 1,
    "stage_key": "publish-0",
    "workflow_sha": "<pinned workflow 40-hex>",
    "tested_head_sha": "<same candidate head 40-hex>",
    "status": "completed",
    "conclusion": "failure"
  }
}
```

同一个 GitHub Actions run/attempt 可以运行整个 DAG。每一轮必须有不同的 `execution.stage_key` 和 `ci.stage_key`，如 `generate-0/publish-0`、`generate-1/publish-1`、`generate-2/publish-2`。也可提供真实数字 `job_id` 替代 `stage_key`；两者都存在时都必须匹配。每轮需要不同的 DSH Session、实际源摘要和新 head，避免把相同输出或空提交计为返工。

`ci.status/conclusion` 在 DAG 运行期间描述该 stage 的独立测试命令是否已经结束，不能冒充整条 workflow 已结束；独立核验器需要核对测试退出码、日志、准确 head、产物和最后 job/workflow 结果。测试失败可以发布候选给网页验收；测试取消、尚未执行或超时不能作为一个已完成的失败用例。

## 验收事件

验收者复制当前候选的 repo/Issue/PR/base/head/criteria/revision 和 CI run/attempt/stage；`previous_event_id` 是当前候选事件的真实 GitHub 评论 ID。候选 1 的 predecessor 是拒绝候选 0 的验收评论 ID，依此类推。仅同一个原 Issue 和 PR 继续返工。

```json
{
  "schema": "harness-rework-journal/v1",
  "kind": "review",
  "loop_id": "DSH-REWORK-002",
  "repository": "WangYu0611/chatgpt-deepseek-harness-test-20261028",
  "issue": 5,
  "pr": 123,
  "revision": 0,
  "base_sha": "<copy candidate base_sha>",
  "criteria_sha256": "<copy candidate criteria_sha256>",
  "head_sha": "<copy candidate head_sha; re-read PR before sending>",
  "previous_event_id": 123456789,
  "ci_run_id": 123456,
  "ci_run_attempt": 1,
  "ci_stage_key": "publish-0",
  "verdict": "FAIL",
  "findings": [
    {"criterion":"arithmetic-correctness","status":"FAIL","detail":"<specific failing assertion and observed values>","evidence_url":"https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/123456"},
    {"criterion":"scope-safety","status":"PASS","detail":"<checked diff and fixed AST scope>","evidence_url":"https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/pull/123/files"},
    {"criterion":"execution-evidence","status":"PASS","detail":"<verified real DSH session, one request, exit and artifact; independent test actually failed>","evidence_url":"https://github.com/WangYu0611/chatgpt-deepseek-harness-test-20261028/actions/runs/123456"}
  ],
  "rework_instruction": "<concrete code correction and exact verification required>",
  "provenance": {
    "automation_id": "<actual reviewer web task ID, only if exposed>",
    "run_id": "<actual hosted run ID, only if exposed>",
    "trigger": "github_pr_event"
  }
}
```

网页任务未暴露自身 ID 时，运行时可省略整个 `provenance`；不能编造。后续只读审计需从官方运行记录独立取得并绑定这些身份；当前审计接口尚不自动补全缺失字段。`provenance` 仅是定位提示，无论写得多像真实都不能独立证明自动来源。

三项 criterion 必须各出现一次，不能遗漏或替换。PASS 要求全部 PASS 且 CI success。FAIL 要求具体 FAIL、可执行返工指令且没有 BLOCKED；证据不足或等待 CI 应 BLOCKED，不派发返工。末轮 PASS 可省略 `rework_instruction`。BLOCKED 为停机状态，恢复需要控制层先对账，不盲目从尾部继续。

## 去重、串行与证明边界

- 完全相同的事件重投或重复评论不产生新轮次，后继必须引用最初的 canonical 评论 ID；同一 ID 换内容、旧 head 的新验收、predecessor 分叉或缺口被拒绝。
- 修订从 0 开始，每次 FAIL 只允许下一个 revision，最多 3 次返工。revision 3 再 FAIL 后 BLOCKED；通过后禁止继续提交。本次用户要求的两轮证明需要 revision 0、1 失败且 revision 2 通过，共 3 个不同 head。
- 本模块不是远程锁，也无法证明 API 分页无遗漏。实际写入者仍须使用单 workflow concurrency/外部串行机制，写前重读 head；未知写入结果先读回对账，不重新调用模型。
- GitHub 评论作者相同只证明账号，不能区分人手、连接器或网页后台任务。真正来源必须从官方网页任务运行记录核验，并关联具体评论、head、run/attempt/stage 与最后通过结果。
- manager-start/arm 是前置启动协议，不在本模块内；本模块从首次已生成且有独立测试结果的 candidate 事件开始。

离线测试命令：`..\.venv\Scripts\python.exe -m unittest tests.test_rework_protocol -v`。测试不会发送 GitHub 请求、运行 DeepSeek 或创建 ChatGPT 任务。
