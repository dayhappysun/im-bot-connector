# 0001. 采用「意图-证据链」提交规范

- **日期**：2026-09-20
- **状态**：accepted
- **影响面**：im-bot-connector-pub 全仓库提交规范

## 背景

AI 生成代码占比快速上升，`git log` 里的信息量却在下降 —— 提交只记录「改了什么」，
不记录「为什么改」「怎么证明对了」「谁（人或 agent）改的」。

一旦未来迁移到 AI native 代码平台，文件树和分支可以重算，但意图与证据无法重建。
当下不留，将来只能考古。

## 决策

im-bot-connector-pub 从本日起，所有新提交遵守《意图-证据链规范》
（全文见 `docs/decisions/0002-intent-evidence-chain-spec.md`）：

1. commit message 必须带 `Intent:` 和 `Verified:` trailer
2. 有 AI 参与的提交必须带 `Agent:` / `Agent-Role:` trailer
3. `Verified` 必须是可重放命令 + 真实输出，禁止「测试通过」这类空话
4. 影响架构方向的改动必须开 ADR（`docs/decisions/NNNN-*.md`）
5. **不追溯重写历史**，只对新提交生效

## 被否决的方案

| 方案 | 否决原因 |
|---|---|
| 一次性重写全部历史提交，补上 Intent | 破坏远端 SHA，所有 clone/fork 失效；收益（历史整齐）远小于代价 |
| 只在 README 里写规范，不做 trailer 强制 | 规范会被忽略；而在 commit 层面的成本几乎为零 |
| 引入 commitlint / commit-msg hook 强制校验 | 有价值，但先跑一段手工期验证格式合理性，避免规则定错后大批返工 |
| 等 AI native 平台成型再迁 | 那时意图链已丢失，迁移 = 重新考古 |

## 后果

**正面**：
- 意图与证据成为可迁移资产，未来平台导入是机械操作
- Agent 参与度可量化、可审计
- 决策理由留在仓库里，不依赖对话记忆

**负面 / 代价**：
- 每次提交多写 3-6 行，短期有摩擦
- 无工具强制，依赖自觉，可能漂移

## 证据

```
$ git log -1 --format='%B' | grep -E '^(Intent|Verified):'
Intent: 建立意图-证据链规范，防止 AI 生成代码的上下文丢失
Verified: 上述命令在规范生效后的首个提交上返回两个 trailer
```

Intent: 把「为什么改 / 怎么证明对了 / 谁改的」从对话记忆固化到 Git 仓库
Verified: 见上方证据块（commit trailer 可被 grep 检索）
