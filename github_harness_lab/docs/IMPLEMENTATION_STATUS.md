# 实施状态与文件映射

记录日期：2026-10-28（Asia/Shanghai）。最新进展：**固定任务的真实 Issue → 本地 Runner → 官方 DSH → 测试 → PR 已通过**，见 [真实流程记录](REAL_ISSUE_DEEPSEEK_PR.md)。下表保留通用原型的阶段边界；本次独立 `live_smoke` 入口不等于已接通通用 Worker。`run --execute` 与 `publish --execute` 仍保持 BLOCKED，网页 Scheduled Tasks 尚未配置。

| 阶段 | 当前交付与主要文件 | 尚未完成 |
| --- | --- | --- |
| P0 仓库与能力核查 | `docs/FEASIBILITY.md`、`LIVE_INTEGRATION.md`：已核查环境和公开能力；独立私有测试仓库的真实 CI 与当前连接器写权限已验证 | 分支保护及网页任务后台审批行为尚未验证 |
| P1 Windows 与官方 Harness | 本机官方 0.2.1-rc.3 已真实调用；官方 Runner 2.338.2 已注册、运行并自动注销；新增受限适配器 | 通用代码执行的 OS/网络隔离、长期服务、供应商金额硬限待验证；本轮只按用户授权限制次数/token/时长 |
| P2 Issue → Worker → PR | `live_smoke.py`、`smoke_policy.py`、`live_tools/` 已完成固定 Issue #3 → 真 DSH → Actions 自动 PR #4 | 通用 Worker 控制桥和任意任务分发未接通；本轮只接固定算术任务 |
| P3 证据系统 | 已取得真实 DSH Session/退出码/usage、源摘要、本地及云测试、PR/head/merge SHA 与 Actions artifact | 通用 Manifest 的完整 JSON Schema validator、图形任务产物和长期证据保留仍待完善 |
| P4 ChatGPT 规划与验收 | `prompts/project-manager.md`、`prompts/pr-reviewer.md`：完整网页提示词已准备 | 未创建网页任务，未验证真实定时/PR 事件、写入与逐次审批限制 |
| P5 幂等、返工与恢复 | `lab_harness/state.py`、`worker.py`、`gitops.py`：本地持久化状态、租约/fencing、中断及 Git 提交恢复、增量返工与上限已实现 | 跨 Runner 的 GitHub journal、远程状态重建和真实写入超时对账尚未接通 |
| P6 验收与演示 | 离线演示已通过；受限真实 Issue→DSH→PR 已通过，本轮新增 44 项针对性测试通过 | 完整 A–F（包括网页验收、自动返工和任意代码隔离）仍 **NOT_VERIFIED** |

## 当前边界

- 超时主要在进程层实施，尚不能保证整个 attempt 的全局硬截止时间。租约和本地状态不是操作系统资源限制。
- 没有运行目录磁盘 quota；日志限制不等于磁盘总量限制，需要另行实施空间预算与受控清理。
- DSH 失败时的 Session 信息目前需人工从受限 stdout 记录中恢复，尚未实现失败 Session 的自动持久化与续接。
- SQLite 是本机恢复缓存，不能取代 GitHub 权威记录。数据库丢失或迁移 Runner 后，当前不能声称会自动从 GitHub 重建。
- 路径检查、环境过滤、clone/worktree 和 outbox 摘要不能证明 OS 隔离、秘密不可读取或发送获得授权。供应商硬预算及真实隔离须独立实测。

## 下一步开发事项

1. 接入只读认证 GitHub preflight：重新获取当前 Issue、可信作者/标签操作者、依赖、分支/PR head 和 Actions 状态，拒绝仅信任事件 JSON。
2. 定义并实现 GitHub journal 协议和有界对账：稳定事件身份、状态版本、跨 Runner 恢复、未知写入结果查询及旧执行实例 fencing。
3. 建立独立可信发布进程/身份，重新校验任务、head、Manifest 和 outbox 来源后，幂等推任务分支、创建/更新同一 PR、发布证据与状态；保持主分支保护。
4. 对接专用隔离执行节点与最小凭据，加入 attempt 全局硬截止、进程树终止、磁盘 quota、失败 Session 检查点和供应商硬预算核验。
5. 补齐完整 JSON Schema 验证和真实 CI/证据来源验证；图形任务另接可复现渲染、图片差异及 GPU/性能数据。
6. 利用已创建的私有测试仓库，在满足执行条件后首次配置两个独立 ChatGPT 网页任务，逐项记录真实 A–F 的 GitHub 证据。用户已授权本次联动测试；当前阻塞是执行条件和能力未验证，不是缺少重复同意。任何能力或权限不足都保留 BLOCKED，不用模拟结果替代。

补充模块：`lab_harness/github.py` 已实现默认断网的 REST 基础客户端，`controller.py` 已实现只读 Issue/PR/head 快照，均以假传输测试，未进行真实 HTTP 调用、未接入 CLI。快照明确不证明标签操作者可信、不提供原子领取或授权发布；上述第 1 项仍需补齐真实来源、依赖、CI 与控制桥集成，第 2–3 项仍未完成。
