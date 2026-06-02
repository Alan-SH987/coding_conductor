# Coding Conductor 改进蓝图

本文档整合了所有已识别的改进建议，提供一个分阶段的实施路线图。

---

## 📊 总览

```
Phase 1 (基础稳固)     Phase 2 (智能增强)     Phase 3 (自主学习)
━━━━━━━━━━━━━━━━━━━    ━━━━━━━━━━━━━━━━━━━    ━━━━━━━━━━━━━━━━━━━
Q3 2026               Q4 2026               2027+

┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│ 错误恢复机制     │   │ 向量检索记忆     │   │ 知识图谱        │
│ 任务队列管控     │   │ 任务标签系统     │   │ 跨项目迁移      │
│ 成本追踪        │   │ AI 冲突解决     │   │ 主动遗忘机制     │
│ 进度可视化      │   │ 历史学习        │   │ 自适应模型选择   │
│ 键盘快捷键      │   │ 依赖关系建模     │   │                 │
└─────────────────┘   └─────────────────┘   └─────────────────┘
```

---

## 🔴 Phase 1: 基础稳固 (高优先级)

**目标**: 提升系统稳定性和用户体验基础

### 1.1 错误恢复和重试机制

**当前状态**: 任务失败后只能重新开始，没有自动重试

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| 可配置自动重试 | 网络超时、临时错误自动重试 (最多 3 次) | ✅ 已完成 |
| 断点续传 | 利用已有 `--resume` 能力从失败点继续 | ✅ 已完成 |
| 错误分类 | 区分可恢复 vs 不可恢复错误 | P1 |
| 失败通知 | 任务失败时发送通知 (webhook/邮件) | P2 |

**实现要点**:
```
backend/
├── app/orchestrator/
│   ├── retry_policy.py      # 重试策略配置
│   ├── error_classifier.py  # 错误分类器
│   └── recovery_manager.py  # 恢复管理器
└── app/api/
    └── webhooks.py          # 通知 webhook
```

**预期收益**: 减少 50%+ 的人工干预需求

---

### 1.2 任务队列和并发控制

**当前状态**: ✅ 已完成

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| 任务队列 | FIFO 或优先级队列 | ✅ 已完成 |
| 并发限制 | 项目级/全局级并发上限 | ✅ 已完成 |
| 资源保护 | 防止同时运行过多任务导致系统过载 | ✅ 已完成 |
| 队列可视化 | 显示队列状态和等待任务 | P1 |

**实现文件**:
```
backend/app/storage/models.py          # QueueEntry, ConcurrencyConfig, QueuePriority
backend/app/orchestrator/queue_manager.py      # TaskQueueManager
backend/app/orchestrator/concurrency_limiter.py # ConcurrencyLimiter
backend/app/orchestrator/service.py            # 集成到 Orchestrator
backend/app/api/orchestration.py               # REST API 端点
backend/tests/test_queue_manager.py            # 单元测试
```

**API 端点**:
- `GET /projects/{id}/queue` - 获取队列状态
- `GET /projects/{id}/queue/tasks` - 列出队列中的任务
- `POST /tasks/{id}/enqueue` - 将任务加入队列
- `POST /tasks/{id}/dequeue` - 从队列移除任务
- `PATCH /tasks/{id}/priority` - 更新任务优先级
- `GET /projects/{id}/concurrency` - 获取并发配置
- `PATCH /projects/{id}/concurrency` - 更新并发配置
- `POST /projects/{id}/queue/process` - 手动触发队列处理

**预期收益**: 系统稳定性提升，资源利用更合理

---

### 1.3 成本追踪与优化

**当前状态**: 无成本追踪能力

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| Token 追踪 | 记录每个任务的 token 消耗 | P0 |
| 成本估算 | 执行前预估成本 | P1 |
| 预算限制 | 设置项目/任务预算上限 | P1 |
| 成本报告 | 按项目/时间段统计成本 | P2 |

**数据模型**:
```python
class TokenUsage:
    id: int
    task_id: int
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    recorded_at: datetime

class CostBudget:
    project_id: int
    daily_limit_usd: float | None
    monthly_limit_usd: float | None
    alert_threshold: float = 0.8  # 80% 时告警
```

**预期收益**: 成本可控，35-50% 成本降低 (配合智能模型选择)

---

### 1.4 进度可视化增强

**当前状态**: 基础进度显示，历史不保留

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| 进度历史保留 | 运行过程信息持久化显示 | ✅ 已完成 |
| 最终总结完整显示 | 总结信息完整保留 | ✅ 已完成 |
| 任务中止按钮 | 运行中任务可中止 | ✅ 已完成 |
| 实时依赖图 | 可视化任务依赖关系 | P1 |
| 并行执行时间线 | 显示并行任务执行状态 | P2 |

**实现要点** (针对未完成部分):
```typescript
// 依赖关系可视化组件
interface DependencyGraph {
  nodes: TaskNode[];
  edges: DependencyEdge[];
  layout: "dagre" | "force" | "tree";
}

// 使用 react-flow 或 d3.js 实现
```

---

### 1.5 键盘快捷键支持

**当前状态**: 纯鼠标操作

**改进内容**:

| 快捷键 | 功能 | 优先级 |
|-------|------|-------|
| `1-9` | 快速选择任务 | P1 |
| `Enter` | 确认选择 | P1 |
| `Escape` | 关闭模态框 | P1 |
| `Ctrl+Enter` | 启动任务 | P2 |
| `Ctrl+K` | 全局命令面板 | P2 |

**实现要点**:
```typescript
// 使用 react-hotkeys-hook
import { useHotkeys } from 'react-hotkeys-hook';

useHotkeys('1,2,3,4,5,6,7,8,9', (e, handler) => {
  const index = parseInt(handler.key) - 1;
  toggleTaskSelection(tasks[index]?.id);
});
```

---

## 🟡 Phase 2: 智能增强 (中期)

**目标**: 引入智能化能力，提升自动化水平

### 2.1 向量检索记忆系统

**当前状态**: 关键词检索 + LLM 蒸馏 (Phase 1 已完成)

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| Embedding 生成 | 为任务/文档生成向量表示 | P0 |
| 向量数据库 | 集成 Chroma/Qdrant/FAISS | P0 |
| 语义检索 | 按相关性而非关键词召回 | P0 |
| 混合检索 | 关键词 + 语义双路召回 | P1 |

**架构设计**:
```
┌─────────────────────────────────────────────────┐
│                  Memory Layer                    │
├─────────────────────────────────────────────────┤
│  ┌─────────────┐        ┌─────────────────┐     │
│  │  Keyword    │        │  Semantic       │     │
│  │  Search     │◄──────►│  Search         │     │
│  │  (BM25)     │        │  (Embeddings)   │     │
│  └─────────────┘        └─────────────────┘     │
│         │                        │              │
│         └────────┬───────────────┘              │
│                  ▼                              │
│         ┌─────────────┐                         │
│         │  Hybrid     │                         │
│         │  Ranker     │                         │
│         └─────────────┘                         │
└─────────────────────────────────────────────────┘
```

**技术选型建议**:
- **轻量级**: ChromaDB (纯 Python，易集成)
- **生产级**: Qdrant (Rust，性能好)
- **Embedding 模型**: text-embedding-3-small (成本效益高)

---

### 2.2 任务标签系统

**当前状态**: 元数据隐含在描述中，无正式 schema

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| 标签定义 | #领域 #技术栈 #复杂度 | P0 |
| 自动标签 | AI 自动打标 | P1 |
| 标签过滤 | 按标签筛选任务 | P1 |
| 标签统计 | 分析项目任务分布 | P2 |

**数据模型**:
```python
class Tag:
    id: int
    name: str  # e.g., "frontend", "typescript"
    category: str  # domain | tech | complexity | custom
    color: str  # 用于 UI 显示

class TaskTag:
    task_id: int
    tag_id: int
    confidence: float  # AI 自动打标的置信度
    source: str  # "manual" | "auto"
```

---

### 2.3 AI 辅助冲突解决

**当前状态**: 检测冲突但需手动解决

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| 简单冲突自动解决 | import 顺序、格式化等 | P1 |
| 复杂冲突建议 | 生成合并建议供审核 | P1 |
| 冲突预防 | 任务分配时预测潜在冲突 | P2 |

**实现要点**:
```python
class ConflictResolver:
    def can_auto_resolve(self, conflict: Conflict) -> bool:
        """判断是否可以自动解决"""
        auto_resolvable = [
            "import_order",
            "whitespace",
            "trailing_comma",
        ]
        return conflict.type in auto_resolvable

    async def generate_suggestion(self, conflict: Conflict) -> str:
        """使用 AI 生成合并建议"""
        prompt = f"""
        Conflict in {conflict.file_path}:

        Version A:
        {conflict.version_a}

        Version B:
        {conflict.version_b}

        Suggest a merged version that preserves both changes.
        """
        return await self.llm.complete(prompt)
```

---

### 2.4 历史学习系统

**当前状态**: 无历史数据利用

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| 成功率追踪 | 记录 域/模型 组合的成功率 | P1 |
| 推荐调整 | 基于历史调整模型推荐 | P1 |
| 模式识别 | 识别常见失败模式 | P2 |

**数据模型**:
```python
class TaskOutcome:
    id: int
    task_id: int
    domain: str
    model_used: str
    success: bool
    execution_time_seconds: int
    token_usage: int
    error_type: str | None  # 如果失败

class ModelRecommendation:
    domain: str
    complexity: str
    recommended_model: str
    confidence: float
    sample_size: int  # 基于多少历史数据
```

---

### 2.5 任务依赖关系建模

**当前状态**: 启发式依赖检测

**改进内容**:

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| 显式依赖声明 | 用户可手动指定依赖 | P1 |
| 文件级依赖 | 分析代码引用关系 | P1 |
| 跨任务依赖 | 支持跨项目依赖 | P2 |
| 动态依赖更新 | 执行时重新评估依赖 | P2 |

**数据模型**:
```python
class TaskDependency:
    id: int
    task_id: int
    depends_on_task_id: int
    dependency_type: str  # explicit | inferred | file_based
    confidence: float

class FileDependency:
    source_file: str
    depends_on_file: str
    dependency_type: str  # import | inheritance | call
```

---

## 🟢 Phase 3: 自主学习 (长期)

**目标**: 构建自适应、自主学习的系统

### 3.1 知识图谱

**描述**: 构建实体关系网络

```
┌─────────┐     uses      ┌─────────┐
│  Task   │──────────────►│  File   │
└─────────┘               └─────────┘
     │                         │
     │ implements              │ belongs_to
     ▼                         ▼
┌─────────┐     contains  ┌─────────┐
│ Feature │◄──────────────│ Module  │
└─────────┘               └─────────┘
```

**核心实体**:
- Task (任务)
- File (文件)
- Module (模块)
- Feature (功能)
- Developer (开发者模式)

**关系类型**:
- `implements`: 任务实现功能
- `modifies`: 任务修改文件
- `depends_on`: 依赖关系
- `similar_to`: 相似任务

---

### 3.2 跨项目经验迁移

**描述**: 在同一组织的多个仓库间共享经验

| 功能 | 描述 |
|-----|------|
| 组织级记忆 | 跨项目共享的最佳实践 |
| 模式库 | 常见问题解决方案模板 |
| 技术栈画像 | 组织偏好的技术选型 |

---

### 3.3 主动遗忘机制

**描述**: 降低噪音，保持记忆质量

| 策略 | 描述 |
|-----|------|
| 时间衰减 | 旧信息权重降低 |
| 相关性过滤 | 低相关内容自动清理 |
| 去重合并 | 相似记忆合并压缩 |
| 手动遗忘 | 用户可标记无用信息 |

---

### 3.4 自适应模型选择

**描述**: 基于实时反馈动态调整模型选择

```python
class AdaptiveModelSelector:
    async def select_model(self, task: Task) -> str:
        # 1. 获取历史数据
        history = await self.get_model_performance(
            domain=task.domain,
            complexity=task.complexity
        )

        # 2. 计算加权得分
        scores = {}
        for model, stats in history.items():
            scores[model] = (
                stats.success_rate * 0.4 +
                (1 / stats.avg_cost) * 0.3 +
                (1 / stats.avg_time) * 0.3
            )

        # 3. 探索与利用平衡 (ε-greedy)
        if random.random() < self.exploration_rate:
            return random.choice(list(scores.keys()))
        return max(scores, key=scores.get)
```

---

## 📅 实施时间线

```
2026 Q3                    2026 Q4                    2027 Q1+
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phase 1                    Phase 2                    Phase 3

Week 1-2: 错误恢复          Week 1-2: 向量数据库集成    知识图谱 MVP
Week 3-4: 任务队列          Week 3-4: Embedding 生成
Week 5-6: 成本追踪          Week 5-6: 标签系统         跨项目迁移
Week 7-8: 进度可视化        Week 7-8: AI 冲突解决
Week 9-10: 键盘快捷键       Week 9-10: 历史学习        遗忘机制
                           Week 11-12: 依赖建模
                                                      自适应选择
```

---

## 📈 预期收益总览

| 阶段 | 收益 |
|-----|------|
| Phase 1 | 系统稳定性 ↑50%，人工干预 ↓50%，用户体验基础完善 |
| Phase 2 | 任务相关性 ↑40%，冲突 ↓80%，开发效率 ↑30% |
| Phase 3 | 自动化水平 ↑60%，跨项目复用 ↑，长期维护成本 ↓ |

---

## 🚀 下一步行动

### 立即可开始 (Phase 1 P0):

1. **错误恢复机制** - 影响大，实现相对简单
   - 创建 `retry_policy.py`
   - 修改任务执行流程添加重试逻辑

2. **任务队列** - 核心基础设施
   - 设计队列数据模型
   - 实现队列管理 API

3. **成本追踪** - 独立功能，可并行开发
   - 添加 TokenUsage 表
   - 集成 token 计数逻辑

### 建议优先级:
```
错误恢复 ━━━━━━━━━━━━━ [高紧急 + 高影响]
任务队列 ━━━━━━━━━━━━━ [高紧急 + 高影响]
成本追踪 ━━━━━━━━━━━   [中紧急 + 高影响]
键盘快捷键 ━━━━━━━━    [低紧急 + 中影响]
进度可视化 ━━━━━━     [低紧急 + 中影响]
```

---

## 附录 A: 技术债务清理

在实施新功能前，建议先处理以下技术债务:

| 债务 | 影响 | 优先级 |
|-----|------|-------|
| 缺少单元测试 | 重构风险高，回归难以发现 | P1 |
| API 文档不完整 | 集成困难，缺少 SDK/客户端指南 | P2 |
| 错误处理不一致 | 调试困难，各模块风格不统一 | P1 |
| 日志格式不统一 | 监控困难，缺少结构化日志 | P2 |

---

## 附录 B: 路线图之外的新建议

这些是在后续审查中发现的额外改进点：

### B.1 CLI 版本兼容层

**风险**: Claude CLI 输出格式变动会破坏适配器

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| CLI 版本检测 | 部分已实现，需完善 | P1 |
| 版本兼容矩阵文档 | 记录支持的 CLI 版本范围 | P2 |
| 格式变更自动告警 | 检测到未知格式时报警 | P2 |

### B.2 运行时健康监控

**当前状态**: 缺少运行时可观测性

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| Agent 进程监控面板 | 实时显示 agent 状态 | P1 |
| 内存/CPU 使用追踪 | 资源使用可视化 | P2 |
| 队列积压告警 | 队列过长时通知 | P2 |

### B.3 增量记忆蒸馏优化

**当前状态**: 全量蒸馏可能随项目增长变慢

| 功能 | 描述 | 优先级 |
|-----|------|-------|
| 增量蒸馏策略 | 仅蒸馏新增/变更内容 | P1 |
| 蒸馏结果缓存 | 避免重复计算 | P1 |
| 变更检测触发 | 按需蒸馏而非定时 | P2 |

---

## 附录 C: 待验证的改进方向

这些方向需要先收集数据再决定是否实施：

### C.1 向量检索记忆 (Phase 2)

**现状评估**: 当前关键词 + LLM 蒸馏的方案可能已足够

**建议**:
- 先收集真实使用数据（检索命中率、用户反馈）
- 评估当前检索效果瓶颈
- 再决定是否引入向量库

### C.2 跨项目经验迁移 (Phase 3)

**现状**: 这是长期目标，但可以先做准备

**近期准备工作**:
- 定义组织级记忆 schema
- 设计迁移 API 契约
- 建立项目间标签统一规范
