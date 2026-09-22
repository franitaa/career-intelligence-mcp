"""Deterministic candidate-to-job matching tests."""

import pytest
from mcp import Client

import candidate_matcher
import server


@pytest.fixture
def profile() -> dict:
    return {
        "candidate": {"name": "Test Candidate"},
        "evidence": [
            {
                "id": "EXP_PRODUCT",
                "type": "experience",
                "organization": "Example Co",
                "role": "Product Engineer",
                "title": None,
                "text": "Led product requirements with engineering and clinical stakeholders.",
                "tags": ["requirements", "stakeholders", "cross-functional", "product management"],
            },
            {
                "id": "SKILL_REGULATORY",
                "type": "skill",
                "organization": None,
                "role": None,
                "title": "Regulatory & Quality",
                "text": "Regulatory & Quality: FDA, CE, ISO 13485",
                "tags": ["regulatory", "FDA", "CE", "ISO 13485"],
            },
            {
                "id": "EXP_RESEARCH",
                "type": "experience",
                "organization": "Research Lab",
                "role": "Researcher",
                "title": None,
                "text": "Supported biomedical research protocols.",
                "tags": ["research", "biomedical"],
            },
        ],
    }


def match(job: dict, profile: dict) -> dict:
    return candidate_matcher.match_candidate_to_job_data(job, profile)


def test_strong_tag_match_and_multiple_evidence_items(profile: dict) -> None:
    result = match(
        {
            "url": "https://example.com/job",
            "title": "Product Engineer",
            "company": "Example Co",
            "required_qualifications": ["FDA, ISO 13485, and regulatory strategy"],
            "responsibilities": ["Work cross-functionally with engineering and clinical stakeholders"],
        },
        profile,
    )

    regulatory = result["requirements"][0]
    responsibility = result["requirements"][1]
    assert regulatory["match_type"] == "strong"
    assert regulatory["matched_evidence"][0]["id"] == "SKILL_REGULATORY"
    assert responsibility["match_type"] == "strong"
    assert responsibility["matched_evidence"][0]["id"] == "EXP_PRODUCT"


def test_partial_keyword_relationship(profile: dict) -> None:
    result = match(
        {"required_qualifications": ["Experience with clinical protocols"], "responsibilities": []},
        profile,
    )

    requirement = result["requirements"][0]
    assert requirement["match_type"] == "partial"
    assert "EXP_RESEARCH" in {item["id"] for item in requirement["matched_evidence"]}


def test_no_evidence_creates_explicit_gap(profile: dict) -> None:
    result = match({"required_qualifications": ["Experience with Kubernetes"]}, profile)

    assert result["requirements"][0]["match_type"] == "none"
    assert result["requirements"][0]["matched_evidence"] == []
    assert result["gaps"] == [{
        "requirement": "Experience with Kubernetes",
        "source": "required_qualifications",
        "reason": "No explicit evidence found in the candidate profile.",
    }]


def test_required_and_preferred_sources_are_preserved(profile: dict) -> None:
    result = match(
        {
            "required_qualifications": ["FDA experience"],
            "preferred_qualifications": ["Biomedical research"],
        },
        profile,
    )

    assert [item["source"] for item in result["requirements"]] == [
        "required_qualifications", "preferred_qualifications"
    ]


def test_skill_based_requirement_and_duplicate_evidence_are_deduplicated(profile: dict) -> None:
    result = match(
        {"skills": ["FDA regulatory FDA regulatory"], "required_qualifications": []},
        profile,
    )

    evidence = result["requirements"][0]["matched_evidence"]
    assert result["requirements"][0]["source"] == "skills"
    assert [item["id"] for item in evidence] == ["SKILL_REGULATORY"]


def test_evidence_text_is_preserved_and_no_overall_score_exists(profile: dict) -> None:
    result = match({"responsibilities": ["Manage product requirements"]}, profile)

    matched = result["requirements"][0]["matched_evidence"][0]
    assert matched["text"] == profile["evidence"][0]["text"]
    assert "score" not in result
    assert "ranking" not in result
    assert "recommendation" not in result


def test_empty_job_fields_return_empty_requirements(profile: dict) -> None:
    result = match(
        {
            "url": None,
            "title": None,
            "company": None,
            "location": None,
            "employment_type": None,
            "description": None,
            "responsibilities": [],
            "required_qualifications": [],
            "preferred_qualifications": [],
            "skills": [],
        },
        profile,
    )

    assert result["requirements"] == []
    assert result["gaps"] == []


def test_malformed_job_input_returns_safe_error(profile: dict) -> None:
    with pytest.raises(candidate_matcher.CandidateMatchError, match="structured job"):
        match("not a job object", profile)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_mcp_tool_is_registered_and_callable(monkeypatch: pytest.MonkeyPatch, profile: dict) -> None:
    monkeypatch.setattr(server, "_load_candidate_profile", lambda: profile)
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        assert any(tool.name == "match_candidate_to_job" for tool in tools.tools)
        result = await client.call_tool(
            "match_candidate_to_job",
            {"job": {"required_qualifications": ["FDA experience"]}},
        )

    assert result.structured_content["requirements"][0]["match_type"] == "strong"
