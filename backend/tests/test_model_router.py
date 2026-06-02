"""Tests for intelligent model routing."""

import pytest
from app.orchestrator.model_router import ModelRouter, TaskComplexity


class TestTaskComplexity:
    """Test task complexity analysis."""

    def test_simple_formatting_task(self):
        assert TaskComplexity.analyze("Fix code formatting") == "fast"
        assert TaskComplexity.analyze("Run prettier on files") == "fast"
        assert TaskComplexity.analyze("Add missing indentation") == "fast"

    def test_simple_documentation_task(self):
        assert TaskComplexity.analyze("Add docstrings to functions") == "fast"
        assert TaskComplexity.analyze("Update README") == "fast"
        assert TaskComplexity.analyze("Add comments to explain code") == "fast"

    def test_simple_rename_task(self):
        assert TaskComplexity.analyze("Rename variable foo to bar") == "fast"
        assert TaskComplexity.analyze("Fix typo in function name") == "fast"

    def test_quick_fix_indicators(self):
        assert TaskComplexity.analyze("Quick fix for logging") == "fast"
        assert TaskComplexity.analyze("Hotfix production bug") == "fast"
        assert TaskComplexity.analyze("Simple change to config") == "fast"

    def test_complex_architecture_task(self):
        result = TaskComplexity.analyze(
            "Refactor authentication system",
            "Need to restructure the auth module for better separation of concerns"
        )
        assert result == "powerful"

    def test_complex_algorithm_task(self):
        result = TaskComplexity.analyze(
            "Optimize database queries",
            "The current query performance is poor and needs algorithm optimization"
        )
        assert result == "powerful"

    def test_complex_integration_task(self):
        result = TaskComplexity.analyze(
            "Integrate payment API",
            "Connect Stripe API with our backend and handle webhooks"
        )
        assert result == "powerful"

    def test_complex_security_task(self):
        result = TaskComplexity.analyze(
            "Fix security vulnerability",
            "SQL injection risk in user input handling"
        )
        assert result == "powerful"

    def test_long_description_indicates_complexity(self):
        long_desc = " ".join(["word"] * 150)
        result = TaskComplexity.analyze("Do something", long_desc)
        assert result == "powerful"

    def test_many_sentences_indicate_complexity(self):
        desc = ". ".join([f"Step {i}" for i in range(10)])
        result = TaskComplexity.analyze("Multi-step task", desc)
        assert result == "powerful"

    def test_short_unclear_task_defaults_to_fast(self):
        assert TaskComplexity.analyze("Fix bug") == "fast"
        assert TaskComplexity.analyze("Update code") == "fast"

    def test_medium_unclear_task_defaults_to_powerful(self):
        result = TaskComplexity.analyze(
            "Implement feature",
            "We need to add this functionality to the system properly"
        )
        assert result == "powerful"


class TestModelRouter:
    """Test model routing logic."""

    def test_complex_routes_to_claude(self):
        router = ModelRouter()
        assert router.select_model("Refactor the architecture") == "claude"

    def test_simple_routes_to_codex(self):
        router = ModelRouter()
        assert router.select_model("Fix formatting") == "codex"

    def test_falls_back_when_preferred_tool_unavailable(self):
        # complex task but only Codex available -> Codex
        router = ModelRouter(available_models=["codex"])
        assert router.select_model("Refactor the architecture") == "codex"
        # simple task but only Claude available -> Claude
        router = ModelRouter(available_models=["claude"])
        assert router.select_model("Fix formatting") == "claude"

    def test_ultimate_fallback_to_any_available(self):
        router = ModelRouter(available_models=["haiku"])
        assert router.select_model("Do something") == "haiku"

    def test_prefer_tier_override(self):
        router = ModelRouter()
        # force the fast tier on a complex task -> Codex
        assert router.select_model("Refactor architecture", prefer_tier="fast") == "codex"

    def test_explain_choice_fast(self):
        router = ModelRouter()
        explanation = router.explain_choice(
            "haiku",
            "Fix formatting"
        )
        assert "straightforward" in explanation.lower()
        assert "formatting" in explanation.lower()

    def test_explain_choice_powerful(self):
        router = ModelRouter()
        explanation = router.explain_choice(
            "sonnet-4-5",
            "Refactor architecture",
            "Need to restructure for better design"
        )
        assert "complex reasoning" in explanation.lower()
        assert "architecture" in explanation.lower()

    def test_all_models_available_by_default(self):
        router = ModelRouter()
        # Should work without filtering
        model1 = router.select_model("Fix formatting")
        model2 = router.select_model("Refactor architecture")
        assert model1 is not None
        assert model2 is not None
        assert model1 != model2  # Different tiers should pick different models
