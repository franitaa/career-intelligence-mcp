"""Tests for the interview-preparation MCP prompt."""

import pytest
from mcp import Client

import server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def get_prompt_text(arguments: dict[str, str] | None = None) -> str:
    async with Client(server.mcp) as client:
        result = await client.get_prompt(
            "interview_prep",
            arguments or {"job_url": "https://jobs.example.com/research-scientist"},
        )
    return "\n".join(
        message.content.text
        for message in result.messages
        if hasattr(message.content, "text")
    )


@pytest.mark.anyio
async def test_interview_prep_prompt_is_registered_with_expected_schema() -> None:
    async with Client(server.mcp) as client:
        prompts = await client.list_prompts()

    prompt = next(prompt for prompt in prompts.prompts if prompt.name == "interview_prep")
    assert prompt.description
    assert prompt.arguments is not None
    assert {argument.name for argument in prompt.arguments} == {"job_url", "additional_context"}
    assert next(argument for argument in prompt.arguments if argument.name == "job_url").required is True
    assert next(argument for argument in prompt.arguments if argument.name == "additional_context").required is False


@pytest.mark.anyio
async def test_interview_prep_prompt_contains_retrieval_and_literature_workflow() -> None:
    text = await get_prompt_text()

    for term in (
        "analyze_job_posting",
        "candidate://profile",
        "match_candidate_to_job",
        "search_pubmed",
        "2-4 substantive biomedical, scientific, or technical topics",
        "Formulate one concise PubMed query",
        "max_results` of 3-5",
        "Synthesize the retrieved citation and abstract metadata",
    ):
        assert term in text


@pytest.mark.anyio
async def test_interview_prep_prompt_preserves_workflow_separation_and_grounding() -> None:
    text = await get_prompt_text(
        {
            "job_url": "https://jobs.example.com/project-leader",
            "additional_context": "This is a first-round interview.",
        }
    )

    assert "This is a first-round interview." in text
    assert "interview preparation only" in text
    assert "Do not invoke the `cover_letter` prompt" in text
    assert "generate a cover letter" in text
    for distinction in ("direct experience", "adjacent knowledge", "newly researched"):
        assert distinction in text
    assert "information" in text
    assert "Do not score" in text
    assert "Do not perform general company web research" in text
    assert "If PubMed is unavailable" in text
    assert "do not fabricate citations" in text


@pytest.mark.anyio
async def test_interview_prep_prompt_excludes_generic_pubmed_topics() -> None:
    text = await get_prompt_text()

    for generic_topic in ("leadership", "communication", "teamwork", "interview skills"):
        assert generic_topic in text
    assert "Do not call PubMed for generic interview or behavioral topics." in text
