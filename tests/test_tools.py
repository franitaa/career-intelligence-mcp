"""In-process MCP tool and resource tests based on the SDK v2 pattern."""

import json
from pathlib import Path

import pytest
from mcp import Client

import server


@pytest.fixture
def anyio_backend() -> str:
	return "asyncio"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(
		server.socket,
		"getaddrinfo",
		lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 80))],
	)


JOB_HTML = """
<html><head><title>Research Scientist</title></head><body>
<h1>Research Scientist</h1><div class="company">Acme Bio</div>
<div class="location">Boston, MA</div>
<article><p>Develop assays and analyze biomedical data for translational research.</p>
<h2>Responsibilities</h2><ul><li>Design experiments</li><li>Analyze results with Python</li></ul>
<h2>Required Qualifications</h2><ul><li>PhD in biology or related field</li></ul>
<h2>Preferred Qualifications</h2><ul><li>Experience with bioinformatics</li></ul></article>
</body></html>
"""

JSON_LD_HTML = """
<html><head>
<script type="application/ld+json">
{
	"@context": "https://schema.org",
	"@type": "JobPosting",
	"title": "Computational Biologist",
	"description": "<p>Study genomic datasets.</p>",
	"hiringOrganization": {"name": "Genome Labs"},
	"jobLocation": {"address": {"addressLocality": "Cambridge", "addressRegion": "MA"}},
	"employmentType": "FULL_TIME",
	"responsibilities": ["Analyze datasets", "Document methods"],
	"qualifications": ["PhD in biology", "Python experience"],
	"skills": "Python, genomics"
}
</script></head><body><h1>Fallback title should not win</h1></body></html>
"""


async def call_tool(url: str) -> dict:
	async with Client(server.mcp) as client:
		result = await client.call_tool("analyze_job_posting", {"url": url})
		return result.structured_content


async def read_candidate_profile() -> str:
	async with Client(server.mcp) as client:
		result = await client.read_resource("candidate://profile")
		return result.contents[0].text


@pytest.mark.anyio
async def test_candidate_profile_resource_returns_profile() -> None:
	content = await read_candidate_profile()
	profile = json.loads(content)

	assert profile["candidate"]["name"] == "Francesca Rondinella"
	assert len(profile["evidence"]) == 53


@pytest.mark.anyio
async def test_candidate_profile_resource_has_expected_json_structure() -> None:
	profile = json.loads(await read_candidate_profile())

	assert set(profile) == {"candidate", "evidence"}
	assert set(profile["candidate"]) == {"name"}
	assert isinstance(profile["evidence"], list)
	assert all({"id", "type", "text", "tags"}.issubset(item) for item in profile["evidence"])


@pytest.mark.anyio
async def test_candidate_profile_resource_missing_file_returns_safe_error(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	monkeypatch.setattr(server, "CANDIDATE_PROFILE_PATH", tmp_path / "missing.json")

	content = await read_candidate_profile()

	assert json.loads(content) == {
		"ok": False,
		"error": {
			"code": "profile_unavailable",
			"message": "The candidate profile is unavailable.",
		},
	}
	assert str(tmp_path) not in content


@pytest.mark.anyio
async def test_valid_url_successful_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
	async def fake_fetch(_: str) -> str:
		return JOB_HTML

	monkeypatch.setattr(server, "_fetch_html", fake_fetch)
	result = await call_tool("https://jobs.example.com/research-scientist")

	assert result == {
		"url": "https://jobs.example.com/research-scientist",
		"title": "Research Scientist",
		"company": "Acme Bio",
		"location": "Boston, MA",
		"employment_type": None,
		"description": "Develop assays and analyze biomedical data for translational research.",
		"responsibilities": ["Design experiments", "Analyze results with Python"],
		"required_qualifications": ["PhD in biology or related field"],
		"preferred_qualifications": ["Experience with bioinformatics"],
		"skills": [],
	}


@pytest.mark.anyio
async def test_json_ld_job_posting_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
	async def fake_fetch(_: str) -> str:
		return JSON_LD_HTML

	monkeypatch.setattr(server, "_fetch_html", fake_fetch)
	result = await call_tool("https://jobs.example.com/computational-biologist")

	assert result["title"] == "Computational Biologist"
	assert result["company"] == "Genome Labs"
	assert result["location"] == "Cambridge, MA"
	assert result["employment_type"] == "FULL_TIME"
	assert result["responsibilities"] == ["Analyze datasets", "Document methods"]
	assert result["skills"] == ["Python, genomics"]


@pytest.mark.anyio
async def test_missing_optional_sections_return_empty_or_null(monkeypatch: pytest.MonkeyPatch) -> None:
	async def fake_fetch(_: str) -> str:
		return "<html><body><h1>Research Assistant</h1><p>Work in a lab.</p></body></html>"

	monkeypatch.setattr(server, "_fetch_html", fake_fetch)
	result = await call_tool("https://jobs.example.com/research-assistant")

	assert result["title"] == "Research Assistant"
	assert result["company"] is None
	assert result["employment_type"] is None
	assert result["responsibilities"] == []
	assert result["skills"] == []


@pytest.mark.anyio
async def test_malformed_html_is_parsed_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
	async def fake_fetch(_: str) -> str:
		return "<html><h1>Engineer<h2>Responsibilities<ul><li>Build systems"

	monkeypatch.setattr(server, "_fetch_html", fake_fetch)
	result = await call_tool("https://jobs.example.com/engineer")

	assert result["title"] == "Engineer"
	assert result["responsibilities"] == ["Build systems"]


@pytest.mark.anyio
async def test_empty_page_returns_insufficient_information(monkeypatch: pytest.MonkeyPatch) -> None:
	async def fake_fetch(_: str) -> str:
		return "<html><body><p></p></body></html>"

	monkeypatch.setattr(server, "_fetch_html", fake_fetch)
	result = await call_tool("https://jobs.example.com/empty")

	assert result["error"]["code"] == "insufficient_information"


@pytest.mark.anyio
async def test_invalid_url(monkeypatch: pytest.MonkeyPatch) -> None:
	async def fail_if_called(_: str) -> str:
		raise AssertionError("HTTP must not be called for invalid URLs")

	monkeypatch.setattr(server, "_fetch_html", fail_if_called)
	result = await call_tool("not a url")
	assert result == {
		"ok": False,
		"error": {"code": "invalid_url", "message": "The URL is malformed."},
		"original_url": "not a url",
	}


@pytest.mark.anyio
async def test_empty_url() -> None:
	result = await call_tool("")
	assert result["error"]["code"] == "invalid_url"


@pytest.mark.anyio
async def test_unsupported_url_scheme() -> None:
	result = await call_tool("file:///tmp/job.html")
	assert result["ok"] is False
	assert result["error"]["code"] == "unsupported_scheme"


@pytest.mark.anyio
async def test_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
	async def failed_fetch(_: str) -> str:
		raise server.JobPostingError("http_error", "The page returned HTTP 503.")

	monkeypatch.setattr(server, "_fetch_html", failed_fetch)
	result = await call_tool("https://jobs.example.com/unavailable")
	assert result["error"] == {"code": "http_error", "message": "The page returned HTTP 503."}


@pytest.mark.anyio
async def test_missing_job_information(monkeypatch: pytest.MonkeyPatch) -> None:
	async def empty_fetch(_: str) -> str:
		return "<html><body><p>Welcome to our website.</p></body></html>"

	monkeypatch.setattr(server, "_fetch_html", empty_fetch)
	result = await call_tool("https://jobs.example.com/empty")
	assert result["error"]["code"] == "insufficient_information"
