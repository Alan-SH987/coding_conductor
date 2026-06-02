"""Intelligent model routing based on task complexity and context.

This module analyzes task descriptions to determine the appropriate model tier
(fast/cheap vs powerful) for optimal cost-performance balance.
"""

from __future__ import annotations

import re
from typing import Optional


class TaskComplexity:
    """Analyzes task complexity to inform model selection."""

    # Keywords indicating simple, straightforward tasks
    SIMPLE_INDICATORS = [
        # Formatting and style
        r"\bformat(?:ting)?\b",
        r"\bstyle\b",
        r"\bindent(?:ation)?\b",
        r"\bprettier\b",
        r"\blint(?:ing)?\b",

        # Documentation
        r"\bcomment(?:s)?\b",
        r"\bdoc(?:string|s|umentation)?\b",
        r"\breadme\b",

        # Simple changes
        r"\brename\b",
        r"\btypo\b",
        r"\bfix\s+typo\b",
        r"\bupdate\s+version\b",
        r"\bchange\s+text\b",
        r"\badd\s+log(?:ging)?\b",

        # Simple additions
        r"\badd\s+print\b",
        r"\badd\s+console\.log\b",
        r"\bremove\s+console\.log\b",
        r"\bdelete\s+unused\b",
    ]

    # Keywords indicating complex tasks
    COMPLEX_INDICATORS = [
        # Architecture and design
        r"\barchitect(?:ure)?\b",
        r"\bdesign\b",
        r"\brefactor\b",
        r"\brestructure\b",
        r"\bmigrat(?:e|ion)\b",

        # Algorithms and logic
        r"\balgorithm\b",
        r"\boptimiz(?:e|ation)\b",
        r"\bperformance\b",
        r"\bcomplex\s+logic\b",

        # System integration
        r"\bintegrat(?:e|ion)\b",
        r"\bapi\b",
        r"\bdatabase\b",
        r"\bauth(?:entication|orization)?\b",

        # Security
        r"\bsecurity\b",
        r"\bvulnerability\b",
        r"\bencrypt(?:ion)?\b",

        # Multi-component changes
        r"\bend[- ]to[- ]end\b",
        r"\bfull[- ]stack\b",
        r"\bacross\s+(?:multiple|several)\b",
    ]

    # Indicators of urgency or simple fixes
    QUICK_FIX_INDICATORS = [
        r"\bquick\s+fix\b",
        r"\bhotfix\b",
        r"\bsmall\s+(?:fix|change)\b",
        r"\bminor\s+(?:fix|change)\b",
        r"\bsimple\b",
        r"\btrivial\b",
    ]

    # Chinese keyword lists — matched as substrings (\b word boundaries don't
    # apply to CJK, and Chinese tasks carry no English keywords at all).
    SIMPLE_CJK = [
        "格式", "缩进", "注释", "文档", "说明", "重命名", "错别字", "拼写",
        "改文字", "日志", "删除无用", "微调", "小改", "措辞",
    ]
    COMPLEX_CJK = [
        "重构", "架构", "设计", "迁移", "算法", "优化", "性能", "集成",
        "数据库", "认证", "授权", "安全", "加密", "端到端", "全栈",
        "并发", "分布式", "重写", "跨服务", "跨多个",
    ]
    QUICK_FIX_CJK = ["快速修", "小修", "热修", "琐碎", "临时", "简单"]

    @classmethod
    def analyze(cls, title: str, description: str = "") -> str:
        """Analyze task complexity and return recommended model tier.

        Returns:
            "fast" for simple tasks that can use cheaper/faster models
            "powerful" for complex tasks that need advanced reasoning
        """
        text = f"{title} {description}".lower()

        # Quick-fix wins outright (English regex or Chinese substring).
        if (any(re.search(p, text, re.IGNORECASE) for p in cls.QUICK_FIX_INDICATORS)
                or any(k in text for k in cls.QUICK_FIX_CJK)):
            return "fast"

        # Count indicators across both languages.
        simple_count = (
            sum(1 for p in cls.SIMPLE_INDICATORS if re.search(p, text, re.IGNORECASE))
            + sum(1 for k in cls.SIMPLE_CJK if k in text)
        )
        complex_count = (
            sum(1 for p in cls.COMPLEX_INDICATORS if re.search(p, text, re.IGNORECASE))
            + sum(1 for k in cls.COMPLEX_CJK if k in text)
        )

        # Length heuristic: count space-words AND CJK characters (Chinese has no
        # spaces, so .split() would undercount a long Chinese task to ~1).
        cjk_chars = len(re.findall(r"[一-鿿]", text))
        word_count = len(text.split()) + cjk_chars
        if word_count > 100:
            complex_count += 1

        # Sentence count across Latin and full-width CJK terminators.
        sentence_count = len(re.split(r"[.!?。！？]+", text))
        if sentence_count > 5:
            complex_count += 1

        if complex_count > simple_count:
            return "powerful"
        if simple_count > 0:
            return "fast"
        # Short, unclear -> fast; medium/long unclear -> powerful (use the
        # stronger tool when in doubt).
        if word_count < 10:
            return "fast"
        return "powerful"


class ModelRouter:
    """Routes a task to the AI tool whose strengths fit it (Claude vs Codex)."""

    # Strength-based tool routing — the whole policy in one line. Flip the mapping
    # to change which tool gets which kind of task.
    #   "powerful" (complex reasoning / architecture / design / refactor) -> claude
    #   "fast"     (straightforward, well-scoped coding / docs / fixes)    -> codex
    TIER_TOOL = {"powerful": "claude", "fast": "codex"}

    def __init__(self, available_models: Optional[list[str]] = None):
        """Initialize router with available models.

        Args:
            available_models: List of model names available. If None, assumes all are available.
        """
        self.available_models = set(available_models) if available_models else None

    def select_model(
        self,
        title: str,
        description: str = "",
        prefer_tier: Optional[str] = None,
    ) -> str:
        """Select the best model for a task.

        Args:
            title: Task title
            description: Task description
            prefer_tier: Optional tier preference ("fast" or "powerful")

        Returns:
            Model name to use
        """
        # Complexity tier -> the tool whose strengths fit it.
        tier = prefer_tier or TaskComplexity.analyze(title, description)
        preferred = self.TIER_TOOL.get(tier, "claude")

        if self.available_models is None or preferred in self.available_models:
            return preferred

        # Preferred tool unavailable: try the other tool, then anything available.
        other = "codex" if preferred == "claude" else "claude"
        if other in self.available_models:
            return other
        return next(iter(sorted(self.available_models)), "claude")

    def explain_choice(self, model: str, title: str, description: str = "") -> str:
        """Explain why a particular model was chosen.

        Args:
            model: The selected model
            title: Task title
            description: Task description

        Returns:
            Human-readable explanation
        """
        tier = TaskComplexity.analyze(title, description)

        if tier == "fast":
            reason = "task appears straightforward (formatting, docs, simple changes)"
        else:
            reason = "task requires complex reasoning (architecture, algorithms, system design)"

        return f"Auto-selected {model}: {reason}"
