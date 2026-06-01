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

    def test_fast_tier_selection(self):
        router = ModelRouter()
        model = router.select_model("Fix formatting")
        assert model in ["haiku", "sonnet-3-5"]

    def test_powerful_tier_selection(self):
        router = ModelRouter()
        model = router.select_model("Refactor architecture")
        assert model in ["sonnet-4-5", "opus"]

    def test_respects_available_models_fast(self):
        router = ModelRouter(available_models=["sonnet-3-5", "opus"])
        model = router.select_model("Fix formatting")
        assert model == "sonnet-3-5"

    def test_respects_available_models_powerful(self):
        router = ModelRouter(available_models=["haiku", "opus"])
        model = router.select_model("Refactor architecture")
        assert model == "opus"

    def test_fallback_to_other_tier(self):
        # Only fast models available, but task is complex
        router = ModelRouter(available_models=["haiku"])
        model = router.select_model("Refactor architecture")
        assert model == "haiku"

    def test_fallback_to_other_tier_reverse(self):
        # Only powerful models available, but task is simple
        router = ModelRouter(available_models=["opus"])
        model = router.select_model("Fix formatting")
        assert model == "opus"

    def test_ultimate_fallback(self):
        # No models match configured tiers
        router = ModelRouter(available_models=["unknown-model"])
        model = router.select_model("Do something")
        assert model == "sonnet-4-5"  # Default fallback

    def test_prefer_tier_override(self):
        router = ModelRouter()
        # Force fast tier even for complex task
        model = router.select_model(
            "Refactor architecture",
            prefer_tier="fast"
        )
        assert model in ["haiku", "sonnet-3-5"]

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
