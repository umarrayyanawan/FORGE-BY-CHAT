"""Unit tests for the FORGE Intent Engine.

Tests cover:
- IntentParser: parse, confidence scoring, missing-field detection
- ClarificationEngine: question generation and answer application
- IntentValidator: required-field and constraint checks
- IntentEngine: full orchestration flow

All LLM calls are mocked.  No external services are required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from system.core.intent.clarification import CLARIFICATION_FIELD_MAP, ClarificationEngine
from system.core.intent.engine import IntentEngine
from system.core.intent.parser import IntentParser
from system.core.intent.schemas import (
    ClarificationQuestion,
    ClarificationRequest,
    ClarificationResponse,
    IntentParseRequest,
    IntentSession,
    IntentStatus,
    ProjectIntent,
)
from system.core.intent.validator import IntentValidator, ValidationResult
from system.shared.exceptions import IntentError, NotFoundError
from system.shared.models import DeployTarget, Platform

pytestmark = pytest.mark.unit

# =========================================================================== #
# Fixtures & helpers
# =========================================================================== #


def _make_llm_response(content: str) -> MagicMock:
    """Construct a mock LLM response object."""
    resp = MagicMock()
    resp.content = content
    return resp


def _make_llm_client(response_content: str) -> AsyncMock:
    """Create an AsyncMock LLM client that always returns the given content."""
    client = AsyncMock()
    client.complete = AsyncMock(return_value=_make_llm_response(response_content))
    return client


def _full_intent_json(raw_prompt: str = "Build CRM for marble suppliers") -> str:
    """Return a JSON string matching the full ProjectIntent schema."""
    return json.dumps(
        {
            "raw_prompt": raw_prompt,
            "industry": "construction materials",
            "product_type": "CRM",
            "platform": "web",
            "deployment_target": "docker",
            "core_features": [
                "Contact and lead management",
                "Quote generation and approval workflow",
                "Inventory tracking for marble slabs",
                "Sales pipeline dashboard",
                "Role-based access control",
            ],
            "integrations": ["QuickBooks", "Google OAuth"],
            "constraints": [],
            "security_requirements": [
                "Multi-factor authentication",
                "Row-level security per user role",
            ],
            "scale_requirements": "Up to 200 concurrent users",
            "target_users": "Marble suppliers and their inside sales teams",
            "tech_preferences": {
                "backend": "FastAPI",
                "frontend": "React",
                "database": "PostgreSQL",
            },
            "timeline": "3 months",
            "budget_range": "$30k–$60k",
        }
    )


def _minimal_intent_json(raw_prompt: str = "Build something") -> str:
    """Return a JSON string with only the raw_prompt field populated."""
    return json.dumps(
        {
            "raw_prompt": raw_prompt,
            "industry": "",
            "product_type": "",
            "platform": "web",
            "deployment_target": "docker",
            "core_features": [],
            "integrations": [],
            "constraints": [],
            "security_requirements": [],
            "scale_requirements": "",
            "target_users": "",
            "tech_preferences": {},
            "timeline": "",
            "budget_range": "",
        }
    )


def _make_full_project_intent(raw_prompt: str = "Build CRM for marble suppliers") -> ProjectIntent:
    """Build a fully-populated ProjectIntent for tests."""
    return ProjectIntent(
        raw_prompt=raw_prompt,
        industry="construction materials",
        product_type="CRM",
        platform=Platform.WEB,
        deployment_target=DeployTarget.DOCKER,
        core_features=[
            "Contact and lead management",
            "Quote generation and approval workflow",
            "Inventory tracking for marble slabs",
            "Sales pipeline dashboard",
            "Role-based access control",
        ],
        integrations=["QuickBooks", "Google OAuth"],
        constraints=[],
        security_requirements=["Multi-factor authentication", "Row-level security per user role"],
        scale_requirements="Up to 200 concurrent users",
        target_users="Marble suppliers and their inside sales teams",
        tech_preferences={"backend": "FastAPI", "frontend": "React", "database": "PostgreSQL"},
        timeline="3 months",
        budget_range="$30k–$60k",
        confidence_score=0.95,
        missing_fields=[],
    )


def _make_partial_project_intent() -> ProjectIntent:
    """Build a partially-populated ProjectIntent."""
    return ProjectIntent(
        raw_prompt="Build something for suppliers",
        industry="construction materials",
        product_type="",
        platform=Platform.WEB,
        deployment_target=DeployTarget.DOCKER,
        core_features=[],
        integrations=[],
        constraints=[],
        security_requirements=[],
        scale_requirements="",
        target_users="",
        tech_preferences={},
        timeline="",
        budget_range="",
        confidence_score=0.15,
        missing_fields=["product_type", "core_features", "target_users"],
    )


# =========================================================================== #
# IntentParser tests
# =========================================================================== #


class TestIntentParser:
    """Tests for IntentParser."""

    @pytest.mark.asyncio
    async def test_parse_simple_prompt_extracts_crm_fields(self) -> None:
        """'Build CRM for marble suppliers' should yield correct industry and product_type."""
        llm_client = _make_llm_client(_full_intent_json())
        parser = IntentParser(llm_client=llm_client)

        intent = await parser.parse("Build CRM for marble suppliers")

        assert intent.industry == "construction materials"
        assert intent.product_type == "CRM"
        assert intent.raw_prompt == "Build CRM for marble suppliers"
        assert len(intent.core_features) >= 3
        assert intent.confidence_score > 0.7
        assert intent.missing_fields == []

    @pytest.mark.asyncio
    async def test_parse_full_prompt_sets_platform_and_deployment(self) -> None:
        """Parsed intent should honour platform and deployment_target from LLM JSON."""
        llm_client = _make_llm_client(_full_intent_json())
        parser = IntentParser(llm_client=llm_client)

        intent = await parser.parse("Build CRM for marble suppliers")

        assert str(intent.platform) == "web"
        assert str(intent.deployment_target) == "docker"

    @pytest.mark.asyncio
    async def test_parse_preserves_raw_prompt(self) -> None:
        """The raw_prompt field must always match the original input regardless of LLM output."""
        original_prompt = "Build CRM for marble suppliers"
        # LLM returns a different raw_prompt — the parser should override it
        llm_response = json.loads(_full_intent_json())
        llm_response["raw_prompt"] = "Something else entirely"
        llm_client = _make_llm_client(json.dumps(llm_response))

        parser = IntentParser(llm_client=llm_client)
        intent = await parser.parse(original_prompt)

        assert intent.raw_prompt == original_prompt

    @pytest.mark.asyncio
    async def test_parse_handles_markdown_fenced_json(self) -> None:
        """Parser should handle LLM responses wrapped in ```json code fences."""
        json_content = _full_intent_json()
        fenced = f"```json\n{json_content}\n```"
        llm_client = _make_llm_client(fenced)

        parser = IntentParser(llm_client=llm_client)
        intent = await parser.parse("Build CRM for marble suppliers")

        assert intent.product_type == "CRM"

    @pytest.mark.asyncio
    async def test_parse_handles_malformed_llm_response(self) -> None:
        """When the LLM returns garbage, parser should return a minimal valid intent."""
        llm_client = _make_llm_client("I cannot parse this request. Please try again.")
        parser = IntentParser(llm_client=llm_client)

        intent = await parser.parse("Build CRM for marble suppliers")

        assert intent.raw_prompt == "Build CRM for marble suppliers"
        assert intent.confidence_score >= 0.0
        assert intent.confidence_score <= 1.0

    @pytest.mark.asyncio
    async def test_parse_raises_intent_error_on_llm_failure(self) -> None:
        """When the LLM client raises an exception, parse() should raise IntentError."""
        client = AsyncMock()
        client.complete = AsyncMock(side_effect=RuntimeError("LLM timeout"))
        parser = IntentParser(llm_client=client)

        with pytest.raises(IntentError, match="LLM intent extraction failed"):
            await parser.parse("Build CRM for marble suppliers")

    def test_calculate_confidence_empty_intent(self) -> None:
        """An intent with no fields populated should have near-zero confidence."""
        parser = IntentParser(llm_client=MagicMock())
        intent = ProjectIntent(raw_prompt="test")

        score = parser._calculate_confidence(intent)

        # No meaningful fields → very low score
        assert score < 0.1

    def test_calculate_confidence_complete_intent(self) -> None:
        """A fully-populated intent should have high confidence (>= 0.7)."""
        parser = IntentParser(llm_client=MagicMock())
        intent = _make_full_project_intent()

        score = parser._calculate_confidence(intent)

        assert score >= 0.7

    def test_calculate_confidence_partial_intent(self) -> None:
        """Partial intent should have intermediate confidence."""
        parser = IntentParser(llm_client=MagicMock())
        intent = ProjectIntent(
            raw_prompt="Build CRM for marble suppliers",
            industry="construction materials",
            product_type="CRM",
            core_features=["Contact management", "Quote generation"],
        )

        score = parser._calculate_confidence(intent)

        # Has 3 fields: industry (0.15), product_type (0.15), core_features (0.20) = 0.50
        assert 0.3 < score < 0.8

    def test_calculate_confidence_is_bounded(self) -> None:
        """Confidence score must always be in [0, 1]."""
        parser = IntentParser(llm_client=MagicMock())

        for intent in [
            ProjectIntent(raw_prompt="x"),
            _make_full_project_intent(),
            _make_partial_project_intent(),
        ]:
            score = parser._calculate_confidence(intent)
            assert 0.0 <= score <= 1.0, f"Score out of bounds: {score}"

    def test_identify_missing_fields_empty_intent(self) -> None:
        """Empty intent should flag all important fields as missing."""
        parser = IntentParser(llm_client=MagicMock())
        intent = ProjectIntent(raw_prompt="test")

        missing = parser._identify_missing_fields(intent)

        # At minimum these critical fields should be missing
        assert "industry" in missing
        assert "product_type" in missing
        assert "core_features" in missing
        assert "target_users" in missing

    def test_identify_missing_fields_full_intent(self) -> None:
        """Fully-populated intent should have no (or very few) missing fields."""
        parser = IntentParser(llm_client=MagicMock())
        intent = _make_full_project_intent()

        missing = parser._identify_missing_fields(intent)

        # All weighted fields should be populated
        assert len(missing) == 0

    def test_identify_missing_fields_partial(self) -> None:
        """Partially-filled intent returns exactly the unpopulated important fields."""
        parser = IntentParser(llm_client=MagicMock())
        intent = ProjectIntent(
            raw_prompt="Build something",
            industry="construction materials",
            product_type="CRM",
            # core_features, target_users, scale_requirements, etc. are missing
        )

        missing = parser._identify_missing_fields(intent)

        assert "core_features" in missing
        assert "target_users" in missing
        # industry and product_type should NOT be missing
        assert "industry" not in missing
        assert "product_type" not in missing

    def test_enrich_sets_status_draft_on_zero_confidence(self) -> None:
        """_enrich should mark a zero-confidence intent as DRAFT."""
        parser = IntentParser(llm_client=MagicMock())
        intent = ProjectIntent(raw_prompt="test")

        enriched = parser._enrich(intent)

        assert enriched.status == IntentStatus.DRAFT
        assert enriched.confidence_score < 0.1

    def test_enrich_sets_status_complete_on_high_confidence(self) -> None:
        """_enrich should mark a high-confidence intent as COMPLETE."""
        parser = IntentParser(llm_client=MagicMock())
        intent = _make_full_project_intent()

        enriched = parser._enrich(intent)

        assert enriched.status == IntentStatus.COMPLETE

    def test_build_parse_prompt_contains_required_guidance(self) -> None:
        """The system prompt must contain extraction guidance for all important fields."""
        parser = IntentParser(llm_client=MagicMock())
        prompt = parser._build_parse_prompt("Build CRM for marble suppliers")

        assert "industry" in prompt
        assert "core_features" in prompt
        assert "platform" in prompt
        assert "deployment_target" in prompt
        assert "FORGE" in prompt
        assert "JSON" in prompt
        assert "Build CRM for marble suppliers" in prompt

    def test_extract_json_from_prose_with_json(self) -> None:
        """_extract_json should pull the JSON object out of surrounding text."""
        parser = IntentParser(llm_client=MagicMock())
        text = 'Here is the result: {"key": "value"} — enjoy!'

        result = parser._extract_json(text)

        assert result == '{"key": "value"}'

    def test_extract_json_from_code_fence(self) -> None:
        """_extract_json should strip code fence markers."""
        parser = IntentParser(llm_client=MagicMock())
        text = '```json\n{"key": "value"}\n```'

        result = parser._extract_json(text)

        assert result == '{"key": "value"}'

    @pytest.mark.asyncio
    async def test_parse_list_field_from_comma_string(self) -> None:
        """Parser should accept comma-separated strings for list fields."""
        llm_response = json.loads(_full_intent_json())
        llm_response["core_features"] = "Contact management, Quote generation, Inventory"
        llm_client = _make_llm_client(json.dumps(llm_response))

        parser = IntentParser(llm_client=llm_client)
        intent = await parser.parse("Build CRM")

        assert isinstance(intent.core_features, list)
        assert len(intent.core_features) == 3


# =========================================================================== #
# ClarificationEngine tests
# =========================================================================== #


class TestClarificationEngine:
    """Tests for ClarificationEngine."""

    @pytest.mark.asyncio
    async def test_generate_questions_returns_max_three(self) -> None:
        """generate_questions should return at most 3 questions."""
        questions_json = json.dumps(
            [
                {
                    "question": "What industry?",
                    "field": "industry",
                    "options": None,
                    "required": True,
                },
                {
                    "question": "What product type?",
                    "field": "product_type",
                    "options": None,
                    "required": True,
                },
                {
                    "question": "Who are the users?",
                    "field": "target_users",
                    "options": None,
                    "required": True,
                },
                {
                    "question": "Extra question?",
                    "field": "core_features",
                    "options": None,
                    "required": False,
                },
            ]
        )
        llm_client = _make_llm_client(questions_json)
        engine = ClarificationEngine(llm_client=llm_client)

        intent = _make_partial_project_intent()
        intent = intent.model_copy(
            update={
                "missing_fields": ["industry", "product_type", "target_users", "core_features"],
            }
        )

        questions = await engine.generate_questions(intent, round_num=0)

        assert len(questions) <= 3

    @pytest.mark.asyncio
    async def test_generate_questions_for_known_missing_fields(self) -> None:
        """Questions should target the actual missing fields."""
        questions_json = json.dumps(
            [
                {
                    "question": "What industry?",
                    "field": "industry",
                    "options": None,
                    "required": True,
                },
                {
                    "question": "What features?",
                    "field": "core_features",
                    "options": None,
                    "required": True,
                },
            ]
        )
        llm_client = _make_llm_client(questions_json)
        engine = ClarificationEngine(llm_client=llm_client)

        intent = ProjectIntent(
            raw_prompt="Build something",
            missing_fields=["industry", "core_features"],
            confidence_score=0.1,
        )

        questions = await engine.generate_questions(intent, round_num=0)

        fields_asked = {q.field for q in questions}
        # Should ask about at least one of the missing fields
        assert fields_asked.intersection({"industry", "core_features"})

    @pytest.mark.asyncio
    async def test_generate_questions_empty_missing_returns_empty(self) -> None:
        """When no fields are missing, no questions should be generated."""
        llm_client = _make_llm_client("[]")
        engine = ClarificationEngine(llm_client=llm_client)
        intent = _make_full_project_intent()
        intent = intent.model_copy(update={"missing_fields": []})

        questions = await engine.generate_questions(intent, round_num=0)

        assert questions == []

    @pytest.mark.asyncio
    async def test_generate_questions_falls_back_to_rule_based_on_llm_failure(self) -> None:
        """When LLM fails, should fall back to deterministic rule-based questions."""
        client = AsyncMock()
        client.complete = AsyncMock(side_effect=RuntimeError("LLM error"))
        engine = ClarificationEngine(llm_client=client)

        intent = ProjectIntent(
            raw_prompt="Build something",
            missing_fields=["industry", "product_type"],
            confidence_score=0.0,
        )

        questions = await engine.generate_questions(intent, round_num=0)

        # Should still return questions using fallback
        assert len(questions) >= 1
        assert all(isinstance(q, ClarificationQuestion) for q in questions)

    @pytest.mark.asyncio
    async def test_apply_answers_updates_string_fields(self) -> None:
        """Answers to string fields should update the corresponding intent fields."""
        llm_client = _make_llm_client("")
        engine = ClarificationEngine(llm_client=llm_client)

        intent = _make_partial_project_intent()
        answers = {
            "product_type": "CRM",
            "target_users": "Marble suppliers and inside sales teams",
        }

        updated = await engine.apply_answers(intent=intent, answers=answers)

        assert updated.product_type == "CRM"
        assert updated.target_users == "Marble suppliers and inside sales teams"

    @pytest.mark.asyncio
    async def test_apply_answers_updates_list_fields(self) -> None:
        """Answers to list fields should be parsed and merged into the list."""
        llm_client = _make_llm_client("")
        engine = ClarificationEngine(llm_client=llm_client)

        intent = ProjectIntent(
            raw_prompt="Build CRM",
            industry="construction",
            core_features=["Existing feature"],
        )

        answers = {
            "core_features": "Contact management, Quote generation, Inventory tracking",
        }

        updated = await engine.apply_answers(intent=intent, answers=answers)

        assert "Contact management" in updated.core_features
        assert "Quote generation" in updated.core_features
        assert "Inventory tracking" in updated.core_features
        # Existing feature preserved
        assert "Existing feature" in updated.core_features

    @pytest.mark.asyncio
    async def test_apply_answers_updates_dict_fields(self) -> None:
        """Answers to tech_preferences should be parsed into {role: tech} dict."""
        llm_client = _make_llm_client("")
        engine = ClarificationEngine(llm_client=llm_client)

        intent = ProjectIntent(raw_prompt="Build CRM")
        answers = {
            "tech_preferences": '{"backend": "FastAPI", "frontend": "React"}',
        }

        updated = await engine.apply_answers(intent=intent, answers=answers)

        assert updated.tech_preferences.get("backend") == "FastAPI"
        assert updated.tech_preferences.get("frontend") == "React"

    @pytest.mark.asyncio
    async def test_apply_answers_recalculates_confidence(self) -> None:
        """After applying answers that fill in missing fields, confidence should rise."""
        llm_client = _make_llm_client("")
        engine = ClarificationEngine(llm_client=llm_client)

        intent = ProjectIntent(
            raw_prompt="Build CRM",
            industry="construction materials",
            confidence_score=0.15,
            missing_fields=["product_type", "core_features", "target_users"],
        )
        answers = {
            "product_type": "CRM",
            "core_features": "Contact management, Lead tracking, Quote generation",
            "target_users": "Sales teams at marble suppliers",
        }

        updated = await engine.apply_answers(intent=intent, answers=answers)

        assert updated.confidence_score > 0.15

    @pytest.mark.asyncio
    async def test_apply_answers_ignores_unknown_fields(self) -> None:
        """Answers for fields not in the schema should be silently ignored."""
        llm_client = _make_llm_client("")
        engine = ClarificationEngine(llm_client=llm_client)

        intent = ProjectIntent(raw_prompt="Build CRM")
        answers = {
            "non_existent_field": "some value",
            "industry": "construction materials",
        }

        # Should not raise
        updated = await engine.apply_answers(intent=intent, answers=answers)
        assert updated.industry == "construction materials"

    def test_rule_based_questions_cover_all_known_fields(self) -> None:
        """_rule_based_questions should produce valid questions for all mapped fields."""
        engine = ClarificationEngine(llm_client=MagicMock())

        for field_name in CLARIFICATION_FIELD_MAP:
            questions = engine._rule_based_questions(None, [field_name])
            assert len(questions) == 1
            assert questions[0].field == field_name
            assert questions[0].question  # non-empty

    def test_prioritise_orders_by_importance(self) -> None:
        """_prioritise should put industry and product_type before budget_range."""
        engine = ClarificationEngine(llm_client=MagicMock())
        shuffled = ["budget_range", "industry", "core_features", "timeline", "product_type"]

        ordered = engine._prioritise(shuffled)

        assert ordered.index("industry") < ordered.index("budget_range")
        assert ordered.index("product_type") < ordered.index("timeline")
        assert ordered.index("core_features") < ordered.index("budget_range")

    def test_split_list_answer_handles_comma_separated(self) -> None:
        engine = ClarificationEngine(llm_client=MagicMock())
        result = engine._split_list_answer("Contact management, Lead tracking, Quote generation")
        assert result == ["Contact management", "Lead tracking", "Quote generation"]

    def test_split_list_answer_handles_newline_separated(self) -> None:
        engine = ClarificationEngine(llm_client=MagicMock())
        result = engine._split_list_answer("Contact management\nLead tracking\nQuote generation")
        assert result == ["Contact management", "Lead tracking", "Quote generation"]

    def test_split_list_answer_handles_json_array(self) -> None:
        engine = ClarificationEngine(llm_client=MagicMock())
        result = engine._split_list_answer('["Contact management", "Lead tracking"]')
        assert result == ["Contact management", "Lead tracking"]

    def test_split_list_answer_single_item(self) -> None:
        engine = ClarificationEngine(llm_client=MagicMock())
        result = engine._split_list_answer("Contact management")
        assert result == ["Contact management"]


# =========================================================================== #
# IntentValidator tests
# =========================================================================== #


class TestIntentValidator:
    """Tests for IntentValidator (synchronous structural checks)."""

    @pytest.mark.asyncio
    async def test_validate_complete_intent_passes(self) -> None:
        """A fully-populated intent should pass validation."""
        validator = IntentValidator(llm_client=None)
        intent = _make_full_project_intent()

        result = await validator.validate(intent)

        assert result.is_valid is True
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_validate_missing_industry_fails(self) -> None:
        """Missing industry should cause a validation error."""
        validator = IntentValidator(llm_client=None)
        intent = _make_full_project_intent()
        intent = intent.model_copy(update={"industry": ""})

        with pytest.raises(IntentError):
            await validator.validate(intent)

    @pytest.mark.asyncio
    async def test_validate_missing_product_type_fails(self) -> None:
        """Missing product_type should cause a validation error."""
        validator = IntentValidator(llm_client=None)
        intent = _make_full_project_intent()
        intent = intent.model_copy(update={"product_type": ""})

        with pytest.raises(IntentError):
            await validator.validate(intent)

    @pytest.mark.asyncio
    async def test_validate_empty_core_features_fails(self) -> None:
        """Empty core_features should cause a validation error."""
        validator = IntentValidator(llm_client=None)
        intent = _make_full_project_intent()
        intent = intent.model_copy(update={"core_features": []})

        with pytest.raises(IntentError):
            await validator.validate(intent)

    @pytest.mark.asyncio
    async def test_validate_missing_target_users_warns(self) -> None:
        """Missing target_users should produce a warning, not an error."""
        validator = IntentValidator(llm_client=None)
        intent = _make_full_project_intent()
        intent = intent.model_copy(update={"target_users": ""})

        # Should not raise (warning only)
        result = await validator.validate(intent)

        assert result.is_valid is True
        assert any("target_users" in w or "user" in w.lower() for w in result.warnings)

    @pytest.mark.asyncio
    async def test_validate_vercel_with_desktop_platform_fails(self) -> None:
        """Vercel deployment is incompatible with desktop platform."""
        validator = IntentValidator(llm_client=None)
        intent = _make_full_project_intent()
        intent = intent.model_copy(
            update={
                "deployment_target": DeployTarget.VERCEL,
                "platform": Platform.DESKTOP,
            }
        )

        with pytest.raises(IntentError, match="not compatible"):
            await validator.validate(intent)

    @pytest.mark.asyncio
    async def test_validate_offline_constraint_with_cloud_deployment_fails(self) -> None:
        """Offline constraint with a cloud-only target should raise IntentError."""
        validator = IntentValidator(llm_client=None)
        intent = _make_full_project_intent()
        intent = intent.model_copy(
            update={
                "constraints": ["must run offline"],
                "deployment_target": DeployTarget.AWS,
            }
        )

        with pytest.raises(IntentError):
            await validator.validate(intent)

    @pytest.mark.asyncio
    async def test_validate_kubernetes_without_scale_warns(self) -> None:
        """Kubernetes deployment without scale requirements should warn."""
        validator = IntentValidator(llm_client=None)
        intent = _make_full_project_intent()
        intent = intent.model_copy(
            update={
                "deployment_target": DeployTarget.KUBERNETES,
                "scale_requirements": "",
            }
        )

        result = await validator.validate(intent)

        assert result.is_valid is True
        assert any("Kubernetes" in w or "kubernetes" in w.lower() for w in result.warnings)

    @pytest.mark.asyncio
    async def test_validate_with_llm_coherence_check(self) -> None:
        """Coherence check via LLM should add warnings/suggestions but not fail validation."""
        coherence_response = json.dumps(
            {
                "coherent": True,
                "issues": [],
                "suggestions": ["Consider adding an analytics dashboard"],
            }
        )
        llm_client = _make_llm_client(coherence_response)
        validator = IntentValidator(llm_client=llm_client)
        intent = _make_full_project_intent()

        result = await validator.validate(intent)

        assert result.is_valid is True
        assert any("analytics" in s.lower() for s in result.suggestions)

    @pytest.mark.asyncio
    async def test_validate_coherence_issues_become_warnings(self) -> None:
        """LLM coherence issues should appear as warnings, not errors."""
        coherence_response = json.dumps(
            {
                "coherent": False,
                "issues": ["A blockchain integration seems out of place for a CRM"],
                "suggestions": [],
            }
        )
        llm_client = _make_llm_client(coherence_response)
        validator = IntentValidator(llm_client=llm_client)
        intent = _make_full_project_intent()

        result = await validator.validate(intent)

        assert result.is_valid is True  # Coherence issues are warnings, not errors
        assert any("blockchain" in w.lower() or "coherence" in w.lower() for w in result.warnings)

    def test_validation_result_add_error_sets_invalid(self) -> None:
        """Adding an error to ValidationResult should set is_valid=False."""
        result = ValidationResult()
        assert result.is_valid is True

        result.add_error("Something critical failed.")

        assert result.is_valid is False
        assert "Something critical failed." in result.errors

    def test_validation_result_add_warning_does_not_invalidate(self) -> None:
        """Adding a warning should not change is_valid."""
        result = ValidationResult()
        result.add_warning("Minor issue.")

        assert result.is_valid is True
        assert "Minor issue." in result.warnings


# =========================================================================== #
# IntentEngine orchestration tests
# =========================================================================== #


class TestIntentEngine:
    """Tests for the IntentEngine orchestrator."""

    def _make_engine(self, llm_content: str) -> tuple[IntentEngine, MagicMock, AsyncMock]:
        """Build an IntentEngine with all dependencies mocked."""
        llm_client = _make_llm_client(llm_content)

        # Mock Redis
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.setex = AsyncMock(return_value=True)
        redis.sadd = AsyncMock(return_value=1)
        redis.expire = AsyncMock(return_value=True)
        redis.delete = AsyncMock(return_value=1)

        # Mock DB session
        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        db.execute = AsyncMock(return_value=mock_result)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.add = MagicMock()

        engine = IntentEngine(llm_client=llm_client, redis=redis, db=db)
        return engine, redis, db

    @pytest.mark.asyncio
    async def test_process_high_confidence_returns_complete(self) -> None:
        """A well-described prompt should bypass clarification and return is_complete=True."""
        engine, _, _ = self._make_engine(_full_intent_json())

        request = IntentParseRequest(prompt="Build CRM for marble suppliers")
        response = await engine.process(request)

        assert response.is_complete is True
        assert response.clarification_needed is False
        assert response.session_id is not None
        assert response.project_id is not None

    @pytest.mark.asyncio
    async def test_process_low_confidence_returns_clarification(self) -> None:
        """A vague prompt should trigger clarification questions."""
        # LLM returns minimal intent (low confidence)
        questions_json = json.dumps(
            [
                {
                    "question": "What industry?",
                    "field": "industry",
                    "options": None,
                    "required": True,
                },
                {
                    "question": "What product type?",
                    "field": "product_type",
                    "options": None,
                    "required": True,
                },
            ]
        )

        llm_client = AsyncMock()
        # First call: parse → minimal intent
        # Second call: generate questions
        llm_client.complete = AsyncMock(
            side_effect=[
                _make_llm_response(_minimal_intent_json("Build something")),
                _make_llm_response(questions_json),
            ]
        )

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.setex = AsyncMock(return_value=True)
        redis.sadd = AsyncMock(return_value=1)
        redis.expire = AsyncMock(return_value=True)

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=mock_result)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.add = MagicMock()

        engine = IntentEngine(llm_client=llm_client, redis=redis, db=db)
        request = IntentParseRequest(prompt="Build something")
        response = await engine.process(request)

        assert response.clarification_needed is True
        assert response.is_complete is False
        assert response.clarification_request is not None
        assert len(response.clarification_request.questions) >= 1

    @pytest.mark.asyncio
    async def test_process_creates_new_session_when_none_provided(self) -> None:
        """process() without session_id should create a new session."""
        engine, _, _ = self._make_engine(_full_intent_json())
        request = IntentParseRequest(prompt="Build CRM for marble suppliers")

        response = await engine.process(request)

        assert response.session_id is not None
        # Must be a valid UUID-like string
        uuid.UUID(response.session_id)

    @pytest.mark.asyncio
    async def test_process_uses_provided_project_id(self) -> None:
        """When project_id is provided, the response should use the same project_id."""
        engine, _, _ = self._make_engine(_full_intent_json())
        project_id = str(uuid.uuid4())
        request = IntentParseRequest(prompt="Build CRM for marble suppliers", project_id=project_id)

        response = await engine.process(request)

        assert response.project_id == project_id

    @pytest.mark.asyncio
    async def test_clarify_raises_not_found_for_unknown_session(self) -> None:
        """clarify() with a non-existent session_id should raise NotFoundError."""
        engine, redis, _ = self._make_engine(_full_intent_json())
        redis.get = AsyncMock(return_value=None)  # Session not in Redis

        clarify_request = ClarificationResponse(
            session_id="non-existent-session-id",
            answers={"industry": "construction"},
        )

        with pytest.raises(NotFoundError):
            await engine.clarify(clarify_request)

    @pytest.mark.asyncio
    async def test_clarify_applies_answers_and_increments_round(self) -> None:
        """clarify() should apply answers and increment the clarification_round."""
        # Set up an existing session in Redis
        session = IntentSession(
            session_id="test-session-123",
            project_id="test-project-456",
            raw_prompt="Build something",
            intent=_make_partial_project_intent(),
            clarification_round=0,
            clarification_history=[],
            status=IntentStatus.CLARIFYING,
        )

        llm_client = _make_llm_client(_full_intent_json("Build something"))

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=session.model_dump_json().encode())
        redis.setex = AsyncMock(return_value=True)
        redis.sadd = AsyncMock(return_value=1)
        redis.expire = AsyncMock(return_value=True)

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=mock_result)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.add = MagicMock()

        engine = IntentEngine(llm_client=llm_client, redis=redis, db=db)
        clarify_request = ClarificationResponse(
            session_id="test-session-123",
            answers={
                "product_type": "CRM",
                "target_users": "Marble suppliers",
                "core_features": "Contact management, Quote generation",
            },
        )

        response = await engine.clarify(clarify_request)

        # Session should still be linked to the same session_id
        assert response.session_id == "test-session-123"

    @pytest.mark.asyncio
    async def test_get_session_returns_none_for_unknown_id(self) -> None:
        """get_session() with unknown ID should return None."""
        engine, redis, db = self._make_engine("")
        redis.get = AsyncMock(return_value=None)

        db_result = MagicMock()
        db_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=db_result)

        result = await engine.get_session("unknown-id")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_intent_returns_none_for_unknown_project(self) -> None:
        """get_intent() with unknown project_id should return None."""
        engine, redis, db = self._make_engine("")
        redis.get = AsyncMock(return_value=None)

        db_result = MagicMock()
        db_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=db_result)

        result = await engine.get_intent("unknown-project")

        assert result is None

    @pytest.mark.asyncio
    async def test_delete_session_returns_true_when_found(self) -> None:
        """delete_session() should return True when the session is deleted."""
        engine, redis, db = self._make_engine("")
        redis.delete = AsyncMock(return_value=1)

        db_row = MagicMock()
        db_result = MagicMock()
        db_result.scalar_one_or_none = MagicMock(return_value=db_row)
        db.execute = AsyncMock(return_value=db_result)
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        result = await engine.delete_session("some-session-id")

        assert result is True


# =========================================================================== #
# Schema validation tests
# =========================================================================== #


class TestSchemas:
    """Tests for Pydantic schema behaviour."""

    def test_project_intent_clamps_negative_confidence(self) -> None:
        """confidence_score should be clamped to 0.0 even when set to a negative value."""
        intent = ProjectIntent(raw_prompt="test", confidence_score=-0.5)
        assert intent.confidence_score == 0.0

    def test_project_intent_clamps_overflowing_confidence(self) -> None:
        """confidence_score should be clamped to 1.0 when set above 1."""
        intent = ProjectIntent(raw_prompt="test", confidence_score=1.5)
        assert intent.confidence_score == 1.0

    def test_project_intent_coerces_csv_features(self) -> None:
        """core_features can be provided as a comma-separated string."""
        intent = ProjectIntent(raw_prompt="test", core_features="Feat A, Feat B, Feat C")  # type: ignore[arg-type]
        assert intent.core_features == ["Feat A", "Feat B", "Feat C"]

    def test_project_intent_coerces_json_features(self) -> None:
        """core_features can be provided as a JSON array string."""
        intent = ProjectIntent(raw_prompt="test", core_features='["Feat A", "Feat B"]')  # type: ignore[arg-type]
        assert intent.core_features == ["Feat A", "Feat B"]

    def test_clarification_request_caps_to_three_questions(self) -> None:
        """ClarificationRequest should silently truncate to 3 questions."""
        questions = [
            ClarificationQuestion(question=f"Question {i}?", field=f"field_{i}") for i in range(6)
        ]
        req = ClarificationRequest(session_id="s1", questions=questions)
        assert len(req.questions) == 3

    def test_clarification_response_strips_whitespace_from_answers(self) -> None:
        """ClarificationResponse should strip whitespace from answer values."""
        resp = ClarificationResponse(
            session_id="s1",
            answers={"industry": "  construction materials  ", "product_type": "CRM\t"},
        )
        assert resp.answers["industry"] == "construction materials"
        assert resp.answers["product_type"] == "CRM"

    def test_intent_session_has_auto_uuid_id(self) -> None:
        """IntentSession.id should be auto-generated as a UUID string."""
        intent = ProjectIntent(raw_prompt="test")
        session = IntentSession(
            session_id="s1",
            project_id="p1",
            raw_prompt="test",
            intent=intent,
        )
        assert session.id is not None
        uuid.UUID(session.id)  # Must be a valid UUID

    def test_intent_parse_request_rejects_short_prompt(self) -> None:
        """IntentParseRequest should reject prompts shorter than 5 characters."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            IntentParseRequest(prompt="Hi")


# =========================================================================== #
# Integration-style smoke test
# =========================================================================== #


class TestIntentEngineEndToEnd:
    """Lightweight end-to-end test using mocked LLM."""

    @pytest.mark.asyncio
    async def test_full_flow_crm_marble_suppliers(self) -> None:
        """End-to-end: 'Build CRM for marble suppliers' → validated intent."""
        llm_client = _make_llm_client(_full_intent_json("Build CRM for marble suppliers"))

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.setex = AsyncMock(return_value=True)
        redis.sadd = AsyncMock(return_value=1)
        redis.expire = AsyncMock(return_value=True)

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        db.execute = AsyncMock(return_value=mock_result)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.add = MagicMock()

        engine = IntentEngine(llm_client=llm_client, redis=redis, db=db)
        request = IntentParseRequest(prompt="Build CRM for marble suppliers")

        response = await engine.process(request)

        # Should complete in one shot given high-confidence LLM output
        assert response.is_complete is True
        assert response.clarification_needed is False
        assert response.intent.industry == "construction materials"
        assert response.intent.product_type == "CRM"
        assert len(response.intent.core_features) >= 3
        assert response.intent.confidence_score >= 0.7
        assert str(response.intent.status) == IntentStatus.VALIDATED

    @pytest.mark.asyncio
    async def test_full_clarification_loop(self) -> None:
        """End-to-end: vague prompt → clarification round → complete intent."""
        questions_json = json.dumps(
            [
                {
                    "question": "What industry?",
                    "field": "industry",
                    "options": None,
                    "required": True,
                },
                {
                    "question": "What product?",
                    "field": "product_type",
                    "options": None,
                    "required": True,
                },
            ]
        )

        llm_client = AsyncMock()
        # Call 1: parse vague prompt → minimal intent
        # Call 2: generate clarification questions
        # Call 3: parse after answers → rich intent
        llm_client.complete = AsyncMock(
            side_effect=[
                _make_llm_response(_minimal_intent_json("Build something")),
                _make_llm_response(questions_json),
                _make_llm_response(_full_intent_json("Build something")),
            ]
        )

        redis = AsyncMock()
        redis.sadd = AsyncMock(return_value=1)
        redis.expire = AsyncMock(return_value=True)
        redis.setex = AsyncMock(return_value=True)

        # First get returns None (no session yet), subsequent calls return serialised session
        session_store: dict[str, bytes] = {}

        async def redis_get(key: str) -> bytes | None:
            return session_store.get(key)

        async def redis_setex(key: str, ttl: int, value: str) -> bool:
            session_store[key] = value.encode() if isinstance(value, str) else value
            return True

        redis.get = AsyncMock(side_effect=redis_get)
        redis.setex = AsyncMock(side_effect=redis_setex)
        redis.delete = AsyncMock(return_value=1)

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=mock_result)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.add = MagicMock()

        engine = IntentEngine(llm_client=llm_client, redis=redis, db=db)

        # Step 1: Parse
        parse_response = await engine.process(IntentParseRequest(prompt="Build something"))

        if parse_response.is_complete:
            # High-confidence shortcut taken — acceptable
            assert parse_response.intent.confidence_score >= 0.7
            return

        assert parse_response.clarification_needed is True
        session_id = parse_response.session_id

        # Step 2: Clarify
        clarify_response = await engine.clarify(
            ClarificationResponse(
                session_id=session_id,
                answers={
                    "industry": "construction materials",
                    "product_type": "CRM",
                },
            )
        )

        # After clarification with good answers, should now be complete
        assert clarify_response.session_id == session_id
        # Confidence should have improved
        assert clarify_response.intent.confidence_score > parse_response.intent.confidence_score
