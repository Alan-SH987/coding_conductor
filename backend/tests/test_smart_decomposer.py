"""Unit tests for smart task decomposition and parallel execution."""

import pytest

from app.orchestrator.smart_decomposer import (
    ProblemAnalyzer,
    ProblemDomain,
    ResultMerger,
    SmartDecomposer,
    SubtaskSpec,
)


class TestProblemAnalyzer:
    """Test problem domain analysis."""

    def test_frontend_detection(self):
        """Test detection of frontend tasks."""
        domain = ProblemAnalyzer.analyze_domain(
            "Add React component for user profile",
            "Create a new React component using Tailwind CSS"
        )
        assert domain == ProblemDomain.FRONTEND

    def test_backend_detection(self):
        """Test detection of backend tasks."""
        domain = ProblemAnalyzer.analyze_domain(
            "Implement REST API endpoint",
            "Add a new FastAPI route for user authentication"
        )
        assert domain == ProblemDomain.BACKEND

    def test_database_detection(self):
        """Test detection of database tasks."""
        domain = ProblemAnalyzer.analyze_domain(
            "Create database migration",
            "Add new schema for user preferences using SQLAlchemy"
        )
        assert domain == ProblemDomain.DATABASE

    def test_testing_detection(self):
        """Test detection of testing tasks."""
        domain = ProblemAnalyzer.analyze_domain(
            "Write unit tests",
            "Add pytest tests for authentication module"
        )
        assert domain == ProblemDomain.TESTING

    def test_security_detection(self):
        """Test detection of security tasks."""
        domain = ProblemAnalyzer.analyze_domain(
            "Add authentication",
            "Implement JWT token-based authentication with role-based permissions"
        )
        assert domain == ProblemDomain.SECURITY

    def test_general_fallback(self):
        """Test fallback to general domain."""
        domain = ProblemAnalyzer.analyze_domain(
            "Fix the bug",
            "Something is broken"
        )
        assert domain == ProblemDomain.GENERAL

    def test_impact_estimation_high(self):
        """Test high impact estimation."""
        impact = ProblemAnalyzer.estimate_impact(
            "Refactor the entire architecture and migrate to new database schema"
        )
        assert impact == "high"

    def test_impact_estimation_low(self):
        """Test low impact estimation."""
        impact = ProblemAnalyzer.estimate_impact(
            "Add comment to single file and fix typo"
        )
        assert impact == "low"

    def test_impact_estimation_medium(self):
        """Test medium impact estimation."""
        impact = ProblemAnalyzer.estimate_impact(
            "Implement new feature for user settings"
        )
        assert impact == "medium"


class TestSmartDecomposer:
    """Test smart task decomposition."""

    def test_dependency_analysis_test_depends_on_implementation(self):
        """Test that test tasks depend on implementation tasks."""
        decomposer = SmartDecomposer(None, None)

        # Create mock specs
        from app.adapters.base import SubtaskSpec as BaseSubtaskSpec
        specs = [
            BaseSubtaskSpec(
                title="Implement user authentication",
                description="Add login functionality",
                capability="code"
            ),
            BaseSubtaskSpec(
                title="Write tests for authentication",
                description="Add unit tests for login",
                capability="code"
            ),
        ]

        # Analyze dependencies
        depends = decomposer._analyze_dependencies(1, specs[1], specs)

        # Test task should depend on implementation
        assert 0 in depends

    def test_dependency_analysis_doc_depends_on_implementation(self):
        """Test that documentation depends on implementation."""
        decomposer = SmartDecomposer(None, None)

        from app.adapters.base import SubtaskSpec as BaseSubtaskSpec
        specs = [
            BaseSubtaskSpec(
                title="Implement API endpoint",
                description="Create REST endpoint for users",
                capability="code"
            ),
            BaseSubtaskSpec(
                title="Document the API",
                description="Add documentation for user endpoint",
                capability="code"
            ),
        ]

        depends = decomposer._analyze_dependencies(1, specs[1], specs)
        assert 0 in depends

    def test_parallel_batches_no_dependencies(self):
        """Test parallel batch creation with no dependencies."""
        decomposer = SmartDecomposer(None, None)

        specs = [
            SubtaskSpec(
                title="Task 1",
                description="",
                capability="code",
                domain=ProblemDomain.FRONTEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
            SubtaskSpec(
                title="Task 2",
                description="",
                capability="code",
                domain=ProblemDomain.BACKEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
            SubtaskSpec(
                title="Task 3",
                description="",
                capability="code",
                domain=ProblemDomain.DATABASE,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
        ]

        batches = decomposer.get_parallel_batches(specs)

        # All tasks should be in one batch since no dependencies
        assert len(batches) == 1
        assert set(batches[0]) == {0, 1, 2}

    def test_parallel_batches_with_dependencies(self):
        """Test parallel batch creation with dependencies."""
        decomposer = SmartDecomposer(None, None)

        specs = [
            SubtaskSpec(
                title="Task 1",
                description="",
                capability="code",
                domain=ProblemDomain.FRONTEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
            SubtaskSpec(
                title="Task 2",
                description="",
                capability="code",
                domain=ProblemDomain.BACKEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[0],  # Depends on task 1
                estimated_impact="low",
            ),
            SubtaskSpec(
                title="Task 3",
                description="",
                capability="code",
                domain=ProblemDomain.TESTING,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[0, 1],  # Depends on both
                estimated_impact="low",
            ),
        ]

        batches = decomposer.get_parallel_batches(specs)

        # Should be 3 batches due to dependencies
        assert len(batches) == 3
        assert batches[0] == [0]
        assert batches[1] == [1]
        assert batches[2] == [2]

    def test_parallel_batches_partial_dependencies(self):
        """Test parallel batch with partial dependencies."""
        decomposer = SmartDecomposer(None, None)

        specs = [
            SubtaskSpec(
                title="Task 1",
                description="",
                capability="code",
                domain=ProblemDomain.FRONTEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
            SubtaskSpec(
                title="Task 2",
                description="",
                capability="code",
                domain=ProblemDomain.BACKEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
            SubtaskSpec(
                title="Task 3",
                description="",
                capability="code",
                domain=ProblemDomain.TESTING,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[0],  # Only depends on task 1
                estimated_impact="low",
            ),
        ]

        batches = decomposer.get_parallel_batches(specs)

        # Task 1 and 2 should be in first batch, task 3 in second
        assert len(batches) == 2
        assert set(batches[0]) == {0, 1}
        assert batches[1] == [2]


class TestResultMerger:
    """Test result merging and conflict detection."""

    def test_no_conflicts(self):
        """Test when there are no conflicts."""
        diffs = [
            "--- a/file1.py\n+++ b/file1.py\n@@ -1,1 +1,2 @@\n+new line",
            "--- a/file2.py\n+++ b/file2.py\n@@ -1,1 +1,2 @@\n+new line",
        ]
        specs = [
            SubtaskSpec(
                title="Task 1",
                description="",
                capability="code",
                domain=ProblemDomain.FRONTEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
            SubtaskSpec(
                title="Task 2",
                description="",
                capability="code",
                domain=ProblemDomain.BACKEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
        ]

        conflicts = ResultMerger.analyze_conflicts(diffs, specs)
        assert len(conflicts) == 0

    def test_conflict_detection(self):
        """Test conflict detection when same file is modified."""
        diffs = [
            "--- a/file1.py\n+++ b/file1.py\n@@ -1,1 +1,2 @@\n+change from task 1",
            "--- a/file1.py\n+++ b/file1.py\n@@ -1,1 +1,2 @@\n+change from task 2",
        ]
        specs = [
            SubtaskSpec(
                title="Task 1",
                description="",
                capability="code",
                domain=ProblemDomain.FRONTEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
            SubtaskSpec(
                title="Task 2",
                description="",
                capability="code",
                domain=ProblemDomain.BACKEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
        ]

        conflicts = ResultMerger.analyze_conflicts(diffs, specs)
        assert len(conflicts) == 1
        assert conflicts[0].file_path == "file1.py"
        assert set(conflicts[0].subtask_indices) == {0, 1}

    def test_high_impact_conflict_severity(self):
        """Test that high impact changes produce blocking conflicts."""
        diffs = [
            "--- a/schema.sql\n+++ b/schema.sql\n@@ -1,1 +1,2 @@\n+migration 1",
            "--- a/schema.sql\n+++ b/schema.sql\n@@ -1,1 +1,2 @@\n+migration 2",
        ]
        specs = [
            SubtaskSpec(
                title="Migration 1",
                description="",
                capability="code",
                domain=ProblemDomain.DATABASE,
                complexity="powerful",
                recommended_model="sonnet-4-5",
                depends_on=[],
                estimated_impact="high",
            ),
            SubtaskSpec(
                title="Migration 2",
                description="",
                capability="code",
                domain=ProblemDomain.DATABASE,
                complexity="powerful",
                recommended_model="sonnet-4-5",
                depends_on=[],
                estimated_impact="high",
            ),
        ]

        conflicts = ResultMerger.analyze_conflicts(diffs, specs)
        assert len(conflicts) == 1
        assert conflicts[0].severity == "blocking"

    def test_auto_merge_strategy_no_conflicts(self):
        """Test auto merge strategy when no conflicts."""
        specs = [
            SubtaskSpec(
                title="Task 1",
                description="",
                capability="code",
                domain=ProblemDomain.FRONTEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
        ]

        strategy = ResultMerger.suggest_merge_strategy([], specs)
        assert strategy["strategy"] == "auto"

    def test_manual_merge_strategy_blocking_conflicts(self):
        """Test manual merge strategy for blocking conflicts."""
        conflicts = [
            ResultMerger.ConflictInfo(
                file_path="schema.sql",
                subtask_indices=[0, 1],
                severity="blocking",
                description="High impact conflict",
            )
        ]
        specs = [
            SubtaskSpec(
                title="Task 1",
                description="",
                capability="code",
                domain=ProblemDomain.DATABASE,
                complexity="powerful",
                recommended_model="sonnet-4-5",
                depends_on=[],
                estimated_impact="high",
            ),
            SubtaskSpec(
                title="Task 2",
                description="",
                capability="code",
                domain=ProblemDomain.DATABASE,
                complexity="powerful",
                recommended_model="sonnet-4-5",
                depends_on=[],
                estimated_impact="high",
            ),
        ]

        strategy = ResultMerger.suggest_merge_strategy(conflicts, specs)
        assert strategy["strategy"] == "manual"

    def test_sequential_merge_strategy_warnings(self):
        """Test sequential merge strategy for warnings."""
        conflicts = [
            ResultMerger.ConflictInfo(
                file_path="utils.py",
                subtask_indices=[0, 1],
                severity="warning",
                description="Medium impact conflict",
            )
        ]
        specs = [
            SubtaskSpec(
                title="Task 1",
                description="",
                capability="code",
                domain=ProblemDomain.BACKEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="medium",
            ),
            SubtaskSpec(
                title="Task 2",
                description="",
                capability="code",
                domain=ProblemDomain.BACKEND,
                complexity="fast",
                recommended_model="haiku",
                depends_on=[],
                estimated_impact="low",
            ),
        ]

        strategy = ResultMerger.suggest_merge_strategy(conflicts, specs)
        assert strategy["strategy"] == "sequential"
        assert "order" in strategy
        # Lower impact should come first
        assert strategy["order"][0] == 1  # Task 2 (low impact)
        assert strategy["order"][1] == 0  # Task 1 (medium impact)
