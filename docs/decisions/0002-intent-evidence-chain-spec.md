# 意图-证据链规范（全文副本）

> 规范全文的权威副本位于仓库内，便于离线和迁移时读取。
> 上游维护位置：`intent-evidence-chain.md`（Hermes 工作区）


> **背景**：AI native 代码平台必然替代 GitHub/GitLab 的"平台层"（review / branch / issue / blame），
> 但 Git 的存储层不会消失。唯一能跨平台迁移的资产不是文件树和分支（这些可以重算），
> 而是 **"为什么这么改 + 怎么证明它是对的"** 这条链。
>
> 本规范把这条链**现在就固化在 Git 里**，让未来任何迁移都是机械操作。
>
> 最后更新：2026-09-20

---

## 一、核心原则

| 原则 | 说明 |
|---|---|
| **提交信息记意图，不记动作** | `fix bug` 是动作；`fix: SSO 回跳后客户端昵称陈旧，因为 profile sync 在返回 user 之前` 是意图 |
| **证据必须可重放** | 写进 commit 的验证命令必须任何人 clone 后能直接跑出同样结论 |
| **AI 参与度显式标记** | 人 / agent 混合提交必须能区分，否则归因链断裂 |
| **决策理由进仓库，不留在对话里** | 会话会被忘掉，`docs/decisions/` 不会 |

---

## 二、Commit Message 格式

```
<type>(<scope>): <意图 —— 为什么这么改，而不是改了什么>

<可选：背景 / 权衡 / 被否决的方案与原因>

<证据块：可重放的验证命令与结果>

<trailers>
```

### 2.1 type 白名单

`feat` `fix` `refactor` `perf` `test` `docs` `chore` `build` `ci` `revert`

### 2.2 强制 Trailer

| Trailer | 必填 | 取值 | 作用 |
|---|---|---|---|
| `Intent` | ✅ | 一句话，≤ 120 字 | 需求来源（"用户要求 X" / "修复线上 Y"） |
| `Verified` | ✅ | 命令 + 结果摘要 | 证据入口，见 §三 |
| `Agent` | ⚠️ 有 AI 参与时必填 | 模型/工具名，如 `hermes/deepseek-flash`、`codex/gpt-5` | 归因 |
| `Agent-Role` | ⚠️ 同上 | `author` / `co-author` / `reviewer` | 人类参与度 |
| `Rejected` | 可选 | 被否决方案 + 一句话原因 | 防止后人重走弯路 |
| `Refs` | 可选 | issue / PR / 飞书文档链接 | 上下文 |

**人类独立完成的提交**：省略 `Agent*` 两个 trailer（诚实标记 > 形式完整）。

### 2.3 完整示例

```
fix(sso): 返回刷新后的 user 对象，避免客户端读到陈旧昵称

背景：登录回跳后前端展示旧 nickname/school，根因是 profile sync 写库在
handler 返回之后执行，响应体里带的是同步前的快照。

权衡：改为 handler 内 await sync 后再查一次 —— 多一次 DB 往返（~8ms），
换取客户端零额外请求。被否决的方案：前端登录后再调一次 /me（多一次
RTT 且需要处理竞态）。

证据：
  $ curl -s -X POST /api/auth/sso -d @fixture.json | jq .user.nickname
  "张三"   # 修改前返回 "old_name"

Intent: 线上用户反馈登录后资料显示为上一版本的昵称
Verified: curl /api/auth/sso → user.nickname 为刷新后值（见上）
Rejected: 前端二次请求 /me —— 多 RTT + 竞态，否决
Agent: hermes/deepseek-flash
Agent-Role: author
```

---

## 三、证据块规范

证据 = **可重放的命令 + 观察到的输出**。三档，至少满足一档：

| 档位 | 适用 | 形式 |
|---|---|---|
| L1 静态 | 纯文档 / 配置 / 样式改动 | `cargo check` / `npm run build` / `bash -n` 的退出码 |
| L2 动态 | 逻辑改动 | 单测 / 集成测试的**实际输出行**（不是"测试通过"四个字） |
| L3 线上 | 生产修复 | `curl` / `dig` / `docker ps` 的真实返回片段 + 时间戳 |

**禁止**：`Verified: 测试通过`、`Verified: 已验证`、`Verified: 手动测试` —— 这三句等于没写。

**好例子**：
```
证据：
  $ cargo test --lib config_field 2>&1 | tail -3
  test result: ok. 14 passed; 0 failed; 0 ignored
```

---

## 四、决策记录（`docs/decisions/`）

影响**架构方向 / 公共接口 / 多模块**的改动，除 commit 外必须留一份 ADR：

- 路径：`docs/decisions/NNNN-<slug>.md`（NNNN 四位递增）
- 模板：见 `docs/decisions/TEMPLATE.md`
- **ADR 写完当次提交进仓库**，不要攒

小型改动只需 commit trailer 里的 `Rejected:`，不必开 ADR。

---

## 五、本规范的自我约束

- 本文件本身受本规范约束：修改它必须写 `Intent` + `Verified`。
- **不追溯**：历史上没有 Intent 的提交不重写，只从本规范生效日起对**新提交**生效。
- 规范演进：改格式要走 ADR（`docs/decisions/0001-intent-evidence-chain.md`）。

---

## 六、为什么值得现在做

1. **迁移成本前置**：未来 AI native 平台导入时，`Intent` 直接映射为"意图流"条目，
   `Verified` 映射为"证据链"，`Rejected` 映射为"被否决方案库"。有这套 trailer，
   迁移是脚本；没有，迁移是重新考古。
2. **Agent 协作可审计**：`Agent` / `Agent-Role` 让"这个 agent 近 30 天多少改动被回滚"
   这类指标成为可能 —— 而这是 AI native 平台的核心竞争力维度。
3. **验证成本正在成为瓶颈**：生成成本趋零后，唯一稀缺的是"证明它对"。
   证据块就是最轻量的验证资产。
4. **成本极低**：一条 trailer 几个字，换来的是不可重建的上下文。

---

## 七、操作约束

- 规范对 **14 个活跃仓库**生效（见 `docs/decisions/` 与各仓库 README 的同步情况）。
- **不重写历史**（`git filter-branch` / `filter-repo` 一律禁止 —— 会破坏远端 SHA）。
- 老提交保持原样。**从生效日起新提交遵守**。
