"""Smart task decomposition with AI model assignment and parallel execution.

This module enhances the basic planning capability with:
1. Intelligent problem analysis and categorization
2. Per-subtask model selection based on complexity and domain
3. Dependency analysis for parallel execution
4. Smart result merging with conflict resolution
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.adapters.base import AgentAdapter
from app.orchestrator.model_router import TaskComplexity


class ProblemDomain(str, Enum):
    """Problem domain categories for specialized model routing."""
    FRONTEND = "frontend"
    BACKEND = "backend"
    DATABASE = "database"
    API = "api"
    TESTING = "testing"
    DEVOPS = "devops"
    ALGORITHM = "algorithm"
    DATA_PROCESSING = "data_processing"
    UI_UX = "ui_ux"
    SECURITY = "security"
    DOCUMENTATION = "documentation"
    REFACTORING = "refactoring"
    FULLSTACK = "fullstack"
    GENERAL = "general"


@dataclass
class SubtaskSpec:
    """Enhanced subtask specification with model assignment and dependencies."""
    title: str
    description: str
    capability: str
    domain: ProblemDomain
    complexity: str  # "fast" or "powerful"
    recommended_model: str
    depends_on: list[int]  # Indices of subtasks this depends on
    estimated_impact: str  # "low", "medium", "high" - for conflict prediction


class ProblemAnalyzer:
    """Analyzes task descriptions to categorize problem domains."""

    DOMAIN_PATTERNS = {
        ProblemDomain.FRONTEND: [
            r"\breact\b", r"\bvue\b", r"\bangular\b", r"\bcomponent\b",
            r"\bui\b", r"\bfrontend\b", r"\bcss\b", r"\bhtml\b",
            r"\btailwind\b", r"\bstyled-components\b",
        ],
        ProblemDomain.BACKEND: [
            r"\bapi\b", r"\bserver\b", r"\bbackend\b", r"\bendpoint\b",
            r"\broute\b", r"\bcontroller\b", r"\bservice\b",
            r"\bmiddleware\b", r"\bexpress\b", r"\bfastapi\b",
        ],
        ProblemDomain.DATABASE: [
            r"\bdatabase\b", r"\bsql\b", r"\bquery\b", r"\bmigration\b",
            r"\bschema\b", r"\bpostgres\b", r"\bmongo\b", r"\bmysql\b",
            r"\borm\b", r"\bsqlalchemy\b", r"\bprisma\b",
        ],
        ProblemDomain.API: [
            r"\brest\b", r"\bgraphql\b", r"\bapi\b", r"\bendpoint\b",
            r"\bwebhook\b", r"\bhttp\b", r"\brequest\b", r"\bresponse\b",
        ],
        ProblemDomain.TESTING: [
            r"\btest\b", r"\bunit\s+test\b", r"\bintegration\s+test\b",
            r"\be2e\b", r"\bpytest\b", r"\bjest\b", r"\bvitest\b",
            r"\bcoverage\b", r"\bmock\b",
        ],
        ProblemDomain.DEVOPS: [
            r"\bdocker\b", r"\bkubernetes\b", r"\bci/cd\b", r"\bdeploy\b",
            r"\bpipeline\b", r"\bactions\b", r"\bjenkins\b",
            r"\binfrastructure\b", r"\bterraform\b",
        ],
        ProblemDomain.ALGORITHM: [
            r"\balgorithm\b", r"\boptimiz\b", r"\bperformance\b",
            r"\bsort\b", r"\bsearch\b", r"\bdata\s+structure\b",
            r"\bcomplexity\b", r"\bbig\s+o\b",
        ],
        ProblemDomain.SECURITY: [
            r"\bsecurity\b", r"\bauth\b", r"\bencrypt\b", r"\btoken\b",
            r"\bpermission\b", r"\brole\b", r"\bvulnerability\b",
            r"\bsanitize\b", r"\bvalidation\b",
        ],
        ProblemDomain.DOCUMENTATION: [
            r"\bdoc\b", r"\breadme\b", r"\bcomment\b", r"\bjsdoc\b",
            r"\bdocstring\b", r"\bexample\b", r"\bguide\b",
        ],
        ProblemDomain.REFACTORING: [
            r"\brefactor\b", r"\brestructure\b", r"\bclean\s+up\b",
            r"\bsimplify\b", r"\bextract\b", r"\bmodularize\b",
        ],
    }

    @classmethod
    def analyze_domain(cls, title: str, description: str = "") -> ProblemDomain:
        """Identify the primary problem domain."""
        text = f"{title} {description}".lower()

        # Score each domain
        scores = {}
        for domain, patterns in cls.DOMAIN_PATTERNS.items():
            score = sum(
                1 for pattern in patterns
                if re.search(pattern, text, re.IGNORECASE)
            )
            if score > 0:
                scores[domain] = score

        if not scores:
            return ProblemDomain.GENERAL

        # Return highest scoring domain
        return max(scores.items(), key=lambda x: x[1])[0]

    @classmethod
    def estimate_impact(cls, description: str) -> str:
        """Estimate the change impact for conflict prediction."""
        text = description.lower()

        # High impact indicators
        high_impact = [
            r"\barchitecture\b", r"\bschema\b", r"\bmigration\b",
            r"\bbreaking\s+change\b", r"\bmajor\s+refactor\b",
            r"\bacross\s+(?:multiple|many)\b",
        ]

        # Low impact indicators
        low_impact = [
            r"\bcomment\b", r"\bdoc\b", r"\bformat\b",
            r"\btypo\b", r"\blog\b", r"\bsingle\s+file\b",
        ]

        if any(re.search(p, text) for p in high_impact):
            return "high"
        elif any(re.search(p, text) for p in low_impact):
            return "low"
        else:
            return "medium"


class SmartDecomposer:
    """Intelligent task decomposition with model routing."""

    def __init__(self, planner: AgentAdapter, model_router):
        """Initialize with a planning-capable adapter and model router."""
        self.planner = planner
        self.model_router = model_router

    async def decompose_with_analysis(
        self,
        goal: str,
        repo_path: str,
        capabilities: list[str],
    ) -> list[SubtaskSpec]:
        """Decompose task with intelligent model assignment.

        This is a two-phase process:
        1. Use AI to analyze and break down the task
        2. Assign optimal models and analyze dependencies
        """
        # Phase 1: Get initial decomposition from planner
        basic_specs = await self.planner.plan(goal, repo_path, capabilities)

        if not basic_specs:
            return []

        # Phase 2: Enhance each subtask with intelligent analysis
        enhanced_specs = []
        for idx, spec in enumerate(basic_specs):
            # Analyze domain
            domain = ProblemAnalyzer.analyze_domain(spec.title, spec.description)

            # Analyze complexity
            complexity = TaskComplexity.analyze(spec.title, spec.description)

            # Select optimal model for this specific subtask
            recommended_model = self.model_router.select_model(
                spec.title,
                spec.description,
                prefer_tier=complexity,
            )

            # Estimate impact
            impact = ProblemAnalyzer.estimate_impact(spec.description)

            # Analyze dependencies (simple heuristic for now)
            depends_on = self._analyze_dependencies(
                idx, spec, basic_specs
            )

            enhanced_specs.append(SubtaskSpec(
                title=spec.title,
                description=spec.description,
                capability=spec.capability,
                domain=domain,
                complexity=complexity,
                recommended_model=recommended_model,
                depends_on=depends_on,
                estimated_impact=impact,
            ))

        return enhanced_specs

    def _analyze_dependencies(
        self,
        current_idx: int,
        current_spec,
        all_specs: list,
    ) -> list[int]:
        """Analyze dependencies between subtasks.

        Uses heuristics to detect if current subtask depends on others:
        - Testing tasks usually depend on implementation tasks
        - Frontend tasks may depend on API tasks
        - Documentation tasks usually come last
        """
        depends_on = []
        current_text = f"{current_spec.title} {current_spec.description}".lower()

        # Check if current is a test task
        is_test = any(
            pattern in current_text
            for pattern in ["test", "testing", "spec"]
        )

        # Check if current is documentation
        is_doc = any(
            pattern in current_text
            for pattern in ["doc", "readme", "comment"]
        )

        for idx, other_spec in enumerate(all_specs[:current_idx]):
            other_text = f"{other_spec.title} {other_spec.description}".lower()

            # Test tasks depend on implementation tasks
            if is_test and "implement" in other_text:
                depends_on.append(idx)

            # Documentation depends on implementation
            if is_doc and not any(p in other_text for p in ["doc", "readme"]):
                depends_on.append(idx)

            # Look for explicit references (file names, component names)
            # Extract potential identifiers from other task
            words = re.findall(r'\b[A-Za-z_][A-Za-z0-9_]{2,}\b', other_text)
            for word in words:
                if word in current_text and len(word) > 4:
                    depends_on.append(idx)
                    break

        return list(set(depends_on))  # Remove duplicates

    def get_parallel_batches(
        self,
        specs: list[SubtaskSpec],
    ) -> list[list[int]]:
        """Group subtasks into parallel execution batches.

        Returns a list of batches, where each batch contains indices
        of subtasks that can run in parallel.
        """
        n = len(specs)
        batches = []
        completed = set()

        while len(completed) < n:
            # Find all tasks whose dependencies are met
            ready = [
                i for i in range(n)
                if i not in completed
                and all(dep in completed for dep in specs[i].depends_on)
            ]

            if not ready:
                # Circular dependency or error - just run remaining serially
                ready = [i for i in range(n) if i not in completed]
                batches.extend([[i] for i in ready])
                break

            batches.append(ready)
            completed.update(ready)

        return batches


class ResultMerger:
    """Smart merging of multiple subtask results."""

    @dataclass
    class ConflictInfo:
        """Information about a merge conflict."""
        file_path: str
        subtask_indices: list[int]
        severity: str  # "blocking", "warning", "info"
        description: str

    @staticmethod
    def analyze_conflicts(
        diffs: list[str],
        specs: list[SubtaskSpec],
    ) -> list[ConflictInfo]:
        """Analyze potential conflicts between subtask results.

        Parses git diffs to find overlapping file modifications.
        """
        conflicts = []

        # Parse each diff to extract modified files
        file_modifications = {}  # file_path -> list of subtask indices

        for idx, diff in enumerate(diffs):
            # Extract file paths from diff headers
            file_pattern = r'^\+\+\+ b/(.+)$'
            for match in re.finditer(file_pattern, diff, re.MULTILINE):
                file_path = match.group(1)
                if file_path not in file_modifications:
                    file_modifications[file_path] = []
                file_modifications[file_path].append(idx)

        # Find files modified by multiple subtasks
        for file_path, indices in file_modifications.items():
            if len(indices) > 1:
                # Determine severity based on impact
                max_impact = max(
                    specs[i].estimated_impact
                    for i in indices
                    if i < len(specs)
                )

                severity = "blocking" if max_impact == "high" else "warning"

                conflicts.append(ResultMerger.ConflictInfo(
                    file_path=file_path,
                    subtask_indices=indices,
                    severity=severity,
                    description=f"Modified by {len(indices)} subtasks: {', '.join(specs[i].title for i in indices if i < len(specs))}",
                ))

        return conflicts

    @staticmethod
    def suggest_merge_strategy(
        conflicts: list[ConflictInfo],
        specs: list[SubtaskSpec],
    ) -> dict:
        """Suggest a merge strategy for conflicting changes.

        Returns:
            {
                "strategy": "sequential" | "manual" | "auto",
                "order": [subtask_indices] if sequential,
                "conflicts": list of conflict descriptions,
            }
        """
        if not conflicts:
            return {
                "strategy": "auto",
                "order": list(range(len(specs))),
                "conflicts": [],
            }

        # If any blocking conflicts, require manual review
        blocking = [c for c in conflicts if c.severity == "blocking"]
        if blocking:
            return {
                "strategy": "manual",
                "conflicts": [c.description for c in blocking],
                "recommendation": "Review and merge manually due to high-impact conflicts",
            }

        # For warnings, suggest sequential merge based on dependencies
        # Build a merge order respecting dependencies
        completed = set()
        order = []

        for _ in range(len(specs)):
            ready = [
                i for i in range(len(specs))
                if i not in completed
                and all(dep in completed for dep in specs[i].depends_on)
            ]
            if not ready:
                ready = [i for i in range(len(specs)) if i not in completed]

            if ready:
                # Pick the one with lowest impact first
                next_task = min(
                    ready,
                    key=lambda i: (
                        {"low": 0, "medium": 1, "high": 2}.get(
                            specs[i].estimated_impact, 1
                        )
                    )
                )
                order.append(next_task)
                completed.add(next_task)

        return {
            "strategy": "sequential",
            "order": order,
            "conflicts": [c.description for c in conflicts],
            "recommendation": "Merge sequentially to handle conflicts gracefully",
        }
