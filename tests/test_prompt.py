"""Tests for the cover-letter MCP prompt."""

import pytest
from mcp import Client

import server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_cover_letter_prompt_is_registered_with_expected_schema() -> None:
    async with Client(server.mcp) as client:
        prompts = await client.list_prompts()

    prompt = next(prompt for prompt in prompts.prompts if prompt.name == "cover_letter")
    assert prompt.description
    assert prompt.arguments is not None
    assert {argument.name for argument in prompt.arguments} == {"job_url", "additional_context"}
    assert next(argument for argument in prompt.arguments if argument.name == "job_url").required is True


@pytest.mark.anyio
async def test_cover_letter_prompt_contains_required_workflow_and_excludes_pubmed() -> None:
    async with Client(server.mcp) as client:
        result = await client.get_prompt(
            "cover_letter",
            {"job_url": "https://jobs.example.com/research-scientist"},
        )

    text = "\n".join(message.content.text for message in result.messages if hasattr(message.content, "text"))
    assert "analyze_job_posting" in text
    assert "match_candidate_to_job" in text
    assert "loads the local candidate profile internally" in text
    assert "search_pubmed" in text
    assert "Do not call `search_pubmed`" in text
    assert "interview preparation" in text
    assert "bypass URL validation" in text
    assert "arbitrary local files" in text


@pytest.mark.anyio
async def test_cover_letter_prompt_includes_optional_context_without_network_calls() -> None:
    async with Client(server.mcp) as client:
        result = await client.get_prompt(
            "cover_letter",
            {
                "job_url": "https://jobs.example.com/job",
                "additional_context": "Use a concise tone.",
            },
        )

    text = "\n".join(message.content.text for message in result.messages if hasattr(message.content, "text"))
    assert "Use a concise tone." in text
    assert "cover letter only" in text
    assert "search_pubmed" in text
    assert "interview preparation" in text
