# Coding Conductor — MVP Foundation Spec (Step 0–1)

> 实施依据文档。本文件是**设计契约层**，不含实现代码。覆盖 MVP 闭环最硬的三个假设：
> GitOps 隔离（Step 1）、Claude Adapter 归一（Step 2）、`.conductor/` 记忆（Step 6）。

---

## 0. North Star（核心押注）

Coding Conductor 不重新发明 Agent，而是做一层**无凭证的进程编排器**：把已登录的官方 CLI
（`claude`、`codex`）当作 headless 子进程驱动，用 git worktree 做隔离，用仓库内文件做共享记忆，
用人工审批门控制合并。**凭证永远在 CLI 自己手里，Conductor 一行都不碰。**

不可违背的约束：
- 优先复用 CLI 订阅登录态，**不**强制 API key；**不**抓 cookie、**不**做浏览器自动化、**不**违反 ToS。
- Adapter Pattern：每个 agent 实现同一契约。
- 通用工具，不绑定任何特定仓库（支持 Python/TS/React/Next/Rust/Go/Java 及任意 git 项目）。
- Agent 永不碰 main：独立 branch + worktree + diff review + merge gate。
- 默认 Human-in-the-loop，Full-Auto 留到 V2。

---

## 1. MVP 范围（一条纵切片）

目标：一名工程师 ~2–3 周做出可演示的端到端闭环。只做这条链路：

1. FastAPI + SQLite + 最小 Next.js UI
2. 注册 1 个本地 git 项目（填路径）
3. **只接 Claude Code adapter**（shell out `claude -p --output-format stream-json`）
4. UI 手动建任务（暂不自动拆解）
5. 后端建 worktree+branch → 跑 Claude → SSE 实时推日志
6. 捕获 `git diff` 并在 UI 展示
7. **Approve → 合并；Reject → 丢弃 worktree**
8. 基础记忆：读 `.conductor/memory/global.md` 注入、回写 `task_history.jsonl`

暂不做：自动 Planner、Router、多 agent、Reviewer agent、handoff、Codex。

---

## 2. 实施顺序

```
Step 0  脚手架：backend(FastAPI+SQLite) + frontend(Next.js) 跑通空壳
Step 1  GitOps Engine：worktree/branch/diff/merge（带单测）        ← 本文 §A
Step 2  Base Adapter 契约 + Claude Adapter：headless 归一事件流     ← 本文 §C
Step 3  Run Streamer：子进程事件 → SSE → UI 实时日志
Step 4  Orchestrator(单任务状态机)：建任务→建worktree→跑→捕diff→落库
Step 5  Approval Gate + Merge：UI 点 Approve 合并 / Reject 丢弃
Step 6  Memory 基础版：global.md 注入 + task_history.jsonl 回写    ← 本文 §B
        ── MVP 完成，可演示 ──
Step 7  Codex Adapter（验证适配器通用性）
Step 8+ Planner / Router / Reviewer / handoff / 并发（V1）
```

---

## A. GitOps Engine 接口契约（Step 1）

整个 MVP 安全性的地基。契约草图（非实现）：

```python
# 数据结构
WorktreeHandle = { task_id, path, branch, base_sha, created_at }
DiffResult     = { files:[{path, status, additions, deletions}],
                   unified_diff: str, is_empty: bool, stat: str }
MergeResult    = { ok: bool, merged_sha, conflict: bool, conflicted_files:[str] }

class GitOpsEngine:
    # 注册项目时调用一次：校验合法 git 仓库、记录 default_branch
    def inspect_repo(repo_path)            -> { is_git, default_branch, is_dirty, head_sha }

    # 从 base 分支最新提交切出隔离工作区
    def create_worktree(task_id, base="<default>") -> WorktreeHandle
        # git worktree add <wt_path> -b conductor/task-<id> <base_sha>

    # agent 改完后：暂存全部改动并代为 commit 到 task 分支，再产出 diff
    def snapshot_and_diff(handle)         -> DiffResult
        # git add -A && git commit -m "conductor: task-<id>" ; git diff <base_sha>..HEAD

    # 审批通过 → 合并；冲突即停、不自动解决
    def merge_to(handle, target="<default>", strategy="no-ff") -> MergeResult

    # 审批拒绝 / 完成清理
    def remove_worktree(handle, force=True) -> None
        # git worktree remove --force ; git branch -D conductor/task-<id>

    def list_worktrees(repo_path)         -> [WorktreeHandle]   # 启动恢复/孤儿清理
```

**关键决策（MVP 取简单解）：**

| 议题 | MVP 决定 |
|---|---|
| worktree 放哪 | 仓库**外**兄弟目录 `<repo>/../.cc-worktrees/<project>/task-<id>`，绝不放进被追踪路径 |
| agent 不 commit 怎么办 | 由 Engine 代为 `add -A + commit`，保证 diff 可稳定捕获 |
| 合并目标 | MVP 直接合并到 `default_branch`（`--no-ff`）；`integration` 两级合并留 V1 |
| 冲突 | 不自动解决 → `MergeResult.conflict=true` → 任务转 `failed`，等人工 |
| 主仓库脏工作区 | `create_worktree` 前若 `is_dirty` 则拒绝并提示，不擅自 stash |
| 危险操作 | worktree 内不配 push 权限；`push/--force/改 .git/config` 一律拦截 |

**DoD：** 对任意本地 repo 完成「切 worktree → 改文件 → 拿 diff → 合并/丢弃」全闭环，
主分支与主工作区零污染（用临时 repo 写单测验证）。

---

## B. `.conductor/` 初始化逻辑（Step 6 + 项目注册）

注册项目时**幂等**初始化记忆骨架（已存在则不覆盖）：

```
register_project(repo_path):
    assert GitOpsEngine.inspect_repo(repo_path).is_git
    ensure_dir       <repo>/.conductor/memory/
    write_if_absent  global.md        ← 模板（项目名/技术栈/约束 占位）
    write_if_absent  architecture.md  ← 空骨架
    write_if_absent  decisions.md     ← "# Decisions (ADR)\n"
    write_if_absent  handoff.md       ← "# Handoff\n(none)"
    touch_if_absent  task_history.jsonl
    write_if_absent  config.yaml      ← {default_agent: claude, route: {...}}
    ensure_gitignore_entries(repo):
        ".cc-worktrees/"   # 临时工作区不入库
        # 注意：.conductor/ 本身要 commit（记忆需版本化），不 ignore
```

**目录布局（位于目标项目内）：**

```
<project>/.conductor/
  config.yaml
  memory/
    global.md          # 项目概览、术语、约束
    architecture.md    # 架构知识
    decisions.md       # ADR 决策日志（append）
    handoff.md         # agent 间交接当前态
    task_history.jsonl # 每个 run 一行，append-only
    summaries/         # 上下文过长时的滚动摘要（V1）
```

**记忆注入（每次 run 前，写进 worktree）：**

```
build_context_bundle(task):
    pick = global.md  +  相关 architecture 片段  +  handoff.md
    render → <worktree>/CLAUDE.md   # Claude 自动读
    render → <worktree>/AGENTS.md   # Codex 自动读（V1 用）
    # 一个来源，双 CLI 适配
```

**回写（run 结束）：**

```
append task_history.jsonl  ← {task_id, agent, status, cost, diff_stat, ts}
（V1）append decisions.md / 更新 handoff.md
```

**关键决策：** 记忆是**仓库内文件、可版本化、人可读**；DB 只存指针。
MVP 只做 `global.md` 注入 + `task_history.jsonl` 回写，其余文件先建骨架不填。

**DoD：** 对新 repo 跑 `register_project` 后 `.conductor/` 结构齐全、`.gitignore` 正确，
再次调用不破坏已有内容。

---

## C. Claude Adapter 事件映射（Step 2）

驱动 `claude` headless 并把 **stream-json（NDJSON）** 归一成统一事件。

**统一契约（所有 adapter 共享）：**

```
interface AgentAdapter:
    name: str
    capabilities: {plan, code, review, test, explain}
    async run(spec, ctx)  -> AsyncIterator[AgentEvent]
    async resume(session_id, spec) -> AsyncIterator[AgentEvent]
    def supports_resume() -> bool
    def healthcheck()     -> { ok, auth_ok, version }

AgentEvent = message | thinking | tool_use | tool_result
           | diff_ready | final | cost | error
```

**启动命令：**

```
claude -p "<task prompt>" \
  --output-format stream-json --verbose \
  --append-system-prompt "<conductor 角色/边界>" \
  --permission-mode acceptEdits \
  [--resume <session_id>] \
  # cwd = worktree_path（CLAUDE.md 已就位，自动读取）
```

**映射表（CLI 原始事件 → 归一 `AgentEvent`）：**

| claude stream-json | 提取 | → 归一事件 |
|---|---|---|
| `type=system, subtype=init` | `session_id`, model, tools | `message(meta)` + 缓存 session_id |
| `type=assistant` → text block | 文本 | `message` |
| `type=assistant` → thinking block | 思考 | `thinking` |
| `type=assistant` → tool_use block | 工具名/入参 | `tool_use` |
| `type=user` → tool_result block | 工具输出 | `tool_result` |
| `type=result, subtype=success` | `result`, `total_cost_usd`, `duration`, `num_turns` | `final` + `cost` |
| `type=result, subtype=error_*` | 错误信息 | `error` |
| stderr / 非零退出码 | 原文 | `error`（区分 `auth` / `runtime`） |
| —（CLI 不产出）— | GitOps `snapshot_and_diff` 结果 | `diff_ready`（run 收尾由 Orchestrator 合成） |

**要点：**
- `session_id` 从 init 与 result 都能拿，缓存后用于 `--resume`（`supports_resume=true`）。
- **认证检测**：`healthcheck()` = 跑极短 `claude -p "ok"`，拿到 init 即 `auth_ok`；
  失败（stderr 提示未登录）→ UI 标红，提示用户先 `claude login`。Conductor 不碰凭证。
- `diff_ready` 由 Orchestrator 在 run 结束后调用 GitOps 合成——CLI 本身不给 diff。

**DoD：** 对 toy 任务（如"在 README 加一行"）跑通：实时 SSE 看到
`message/tool_use/tool_result/final`，结束后拿到 `cost` 与 `diff_ready`，并能 `--resume` 续接同一 session。

---

## 附：风险与缓解（与本阶段相关）

| 风险 | 缓解 |
|---|---|
| CLI 输出格式变动 | 解析集中在 adapter；优先 JSON 模式；契约测试 + 版本探测 |
| 订阅态 headless 的 ToS/限流 | 只复用已登录会话、不碰凭证；内置节流 + 并发上限 + kill switch |
| agent 执行任意命令 | worktree 隔离 + 权限模式 + 命令白名单；禁 push/force/改 CI |
| worktree/合并冲突 | 每任务独立 worktree+branch；冲突即停等人工 |
| 过度设计拖慢落地 | 严守 §1 切片；队列/向量/多用户推迟到 V2 |
