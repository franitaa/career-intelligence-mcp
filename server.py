"""MCP server for extracting a small, structured view of job postings."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from mcp.server import MCPServer

# MCP server registration follows the official Python SDK v2 examples:
# https://github.com/modelcontextprotocol/python-sdk
from candidate_matcher import CandidateMatchError, match_candidate_to_job_data
from pubmed_search import PubMedError, search_pubmed_data

MAX_URL_LENGTH = 2_048
MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 3
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_FIELD_LENGTH = 4_000
USER_AGENT = "Career-Intelligence-MCP/0.1"
CANDIDATE_PROFILE_PATH = Path(__file__).resolve().parent / "data" / "candidate_profile.json"

mcp = MCPServer(
	"Career Intelligence MCP",
	description="Read-only retrieval and structuring of public job postings. "
	"The server does not apply for jobs, send messages, or bypass access controls.",
)


class JobPostingError(Exception):
	"""An expected, model-readable failure while analyzing a posting."""

	def __init__(self, code: str, message: str) -> None:
		super().__init__(message)
		self.code = code
		self.message = message


class CandidateProfileError(Exception):
	"""An expected, safe failure while loading the candidate evidence resource."""

	def __init__(self, code: str, message: str) -> None:
		super().__init__(message)
		self.code = code
		self.message = message


def _load_candidate_profile() -> dict[str, Any]:
	"""Load and validate the candidate evidence JSON from the data directory."""
	try:
		profile = json.loads(CANDIDATE_PROFILE_PATH.read_text(encoding="utf-8"))
	except FileNotFoundError:
		raise CandidateProfileError("profile_unavailable", "The candidate profile is unavailable.") from None
	except (OSError, UnicodeDecodeError, json.JSONDecodeError):
		raise CandidateProfileError("profile_invalid", "The candidate profile could not be read as valid JSON.") from None

	if not isinstance(profile, dict) or not isinstance(profile.get("candidate"), dict):
		raise CandidateProfileError("profile_invalid", "The candidate profile has an invalid structure.")
	if not isinstance(profile["candidate"].get("name"), str) or not isinstance(profile.get("evidence"), list):
		raise CandidateProfileError("profile_invalid", "The candidate profile has an invalid structure.")

	for evidence in profile["evidence"]:
		if not isinstance(evidence, dict) or not {"id", "type", "text", "tags"}.issubset(evidence):
			raise CandidateProfileError("profile_invalid", "The candidate profile has an invalid structure.")
		if not all(isinstance(evidence[field], str) and evidence[field] for field in ("id", "type", "text")):
			raise CandidateProfileError("profile_invalid", "The candidate profile has an invalid structure.")
		if not isinstance(evidence["tags"], list) or not all(isinstance(tag, str) for tag in evidence["tags"]):
			raise CandidateProfileError("profile_invalid", "The candidate profile has an invalid structure.")

	return profile


def _candidate_profile_error(error: CandidateProfileError) -> str:
	return json.dumps(
		{"ok": False, "error": {"code": error.code, "message": error.message}},
		ensure_ascii=False,
	)


@mcp.resource(
	"candidate://profile",
	mime_type="application/json",
	description=(
		"Read-only ground-truth candidate evidence loaded from the local profile "
		"JSON. This resource does not summarize, rank, score, infer, or rewrite evidence."
	),
)
def candidate_profile() -> str:
	"""Return the validated candidate profile as JSON for LLM retrieval."""
	try:
		return json.dumps(_load_candidate_profile(), ensure_ascii=False, indent=2)
	except CandidateProfileError as error:
		return _candidate_profile_error(error)


def _blocked_ip(address: str) -> bool:
	"""Return whether an address is unsuitable for outbound fetching."""
	ip = ipaddress.ip_address(address)
	return any(
		(
			ip.is_private,
			ip.is_loopback,
			ip.is_link_local,
			ip.is_unspecified,
			ip.is_multicast,
			ip.is_reserved,
		)
	)


def _validate_url(url: str) -> str:
	"""Validate a public HTTP(S) URL and reject SSRF-prone destinations."""
	if not isinstance(url, str) or not url.strip():
		raise JobPostingError("invalid_url", "Provide a non-empty job-posting URL.")
	if len(url) > MAX_URL_LENGTH:
		raise JobPostingError("invalid_url", "The URL is longer than the allowed limit.")

	try:
		parsed = urlsplit(url.strip())
		scheme = parsed.scheme.lower()
		hostname = parsed.hostname
		port = parsed.port
	except ValueError:
		raise JobPostingError("invalid_url", "The URL is malformed.") from None

	if not scheme:
		raise JobPostingError("invalid_url", "The URL is malformed.")
	if scheme not in {"http", "https"}:
		raise JobPostingError("unsupported_scheme", "Only http:// and https:// URLs are allowed.")
	if not hostname or parsed.username or parsed.password:
		raise JobPostingError("invalid_url", "The URL must include a public hostname without credentials.")
	if port is not None and not 1 <= port <= 65_535:
		raise JobPostingError("invalid_url", "The URL contains an invalid port.")

	hostname = hostname.rstrip(".").lower()
	if hostname in {"localhost", "localhost.localdomain"}:
		raise JobPostingError("blocked_destination", "Local destinations are not allowed.")

	try:
		addresses = {str(ipaddress.ip_address(hostname))}
	except ValueError:
		try:
			addresses = {
				str(address[4][0])
				for address in socket.getaddrinfo(
					hostname,
					port or (443 if scheme == "https" else 80),
					type=socket.SOCK_STREAM,
				)
			}
		except (socket.gaierror, OSError):
			raise JobPostingError("inaccessible_page", "The hostname could not be resolved.") from None

	if not addresses or any(_blocked_ip(address) for address in addresses):
		raise JobPostingError("blocked_destination", "Private or local network destinations are not allowed.")
	return parsed.geturl()


async def _fetch_html(url: str) -> str:
	"""Fetch bounded HTML while validating every redirect destination."""
	current_url = url
	timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
	limits = httpx.Limits(max_connections=5, max_keepalive_connections=2)

	try:
		async with httpx.AsyncClient(
			timeout=timeout,
			follow_redirects=False,
			headers={"User-Agent": USER_AGENT},
			limits=limits,
		) as client:
			for _ in range(MAX_REDIRECTS + 1):
				current_url = _validate_url(current_url)
				async with client.stream("GET", current_url) as response:
					if 300 <= response.status_code < 400:
						location = response.headers.get("location")
						if not location:
							raise JobPostingError("http_error", "The page returned an invalid redirect.")
						current_url = urljoin(current_url, location)
						continue
					response.raise_for_status()
					content = bytearray()
					async for chunk in response.aiter_bytes():
						content.extend(chunk)
						if len(content) > MAX_RESPONSE_BYTES:
							raise JobPostingError(
								"resource_limit",
								"The page is larger than the allowed download limit.",
							)
					encoding = response.encoding or "utf-8"
					return bytes(content).decode(encoding, errors="replace")
	except JobPostingError:
		raise
	except httpx.TimeoutException:
		raise JobPostingError("timeout", "The page took too long to respond.") from None
	except httpx.HTTPStatusError as error:
		raise JobPostingError("http_error", f"The page returned HTTP {error.response.status_code}.") from None
	except httpx.RequestError:
		raise JobPostingError("inaccessible_page", "The page could not be reached.") from None

	raise JobPostingError("http_error", "The page redirected too many times.")


def _clean_text(value: str, limit: int = MAX_FIELD_LENGTH) -> str:
	"""Normalize whitespace and cap text returned to the model."""
	return re.sub(r"\s+", " ", value).strip()[:limit]


def _node_text(node: Tag | None, limit: int = MAX_FIELD_LENGTH) -> str:
	return _clean_text(node.get_text(" ", strip=True), limit) if node else ""


def _meta_or_node_text(node: Tag | None) -> str:
	if not node:
		return ""
	if node.name == "meta":
		return _clean_text(str(node.get("content", "")))
	if node.name in {"h1", "h2", "h3", "h4", "h5"}:
		direct_text = _clean_text(" ".join(str(item) for item in node.find_all(string=True, recursive=False)))
		if direct_text:
			return direct_text
	return _node_text(node)


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
	for selector in selectors:
		text = _meta_or_node_text(soup.select_one(selector))
		if text:
			return text
	return ""


def _clean_document(soup: BeautifulSoup) -> None:
	"""Remove page chrome before extracting meaningful job-posting text."""
	for selector in (
		"script", "style", "noscript", "template", "svg", "nav", "header", "footer",
		"[id*='cookie' i]", "[class*='cookie' i]", "[id*='consent' i]", "[class*='consent' i]",
	):
		for node in soup.select(selector):
			node.decompose()


def _json_ld_job_posting(soup: BeautifulSoup) -> dict[str, Any]:
	"""Return the first schema.org JobPosting object found in JSON-LD."""
	for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
		try:
			payload = json.loads(script.string or script.get_text())
		except (json.JSONDecodeError, TypeError):
			continue
		candidates = payload if isinstance(payload, list) else [payload]
		for candidate in candidates:
			if isinstance(candidate, dict) and isinstance(candidate.get("@graph"), list):
				candidates.extend(candidate["@graph"])
		for candidate in candidates:
			if not isinstance(candidate, dict):
				continue
			types = candidate.get("@type", [])
			if isinstance(types, str):
				types = [types]
			if "JobPosting" in types:
				return candidate
	return {}


def _json_ld_text(value: Any) -> str:
	if isinstance(value, dict):
		if "name" in value:
			return _clean_text(str(value["name"]))
		if "address" in value:
			return _json_ld_text(value["address"])
		if "addressLocality" in value:
			return _clean_text(", ".join(str(value.get(key, "")) for key in ("addressLocality", "addressRegion", "addressCountry") if value.get(key)))
	if isinstance(value, list):
		return ", ".join(filter(None, (_json_ld_text(item) for item in value)))
	if value is not None:
		text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
		return _clean_text(text, 8_000)
	return ""


def _json_ld_list(value: Any) -> list[str]:
	if value is None:
		return []
	if isinstance(value, list):
		values = [_json_ld_text(item) for item in value]
	else:
		values = [line for line in re.split(r"\n|\r|•", _json_ld_text(value)) if line.strip()]
	return list(dict.fromkeys(value for value in (_clean_text(item, 2_000) for item in values) if value))[:30]


def _section_text(soup: BeautifulSoup, patterns: tuple[str, ...]) -> list[str]:
	"""Collect list items or paragraphs following a matching heading."""
	for heading in soup.find_all(["h2", "h3", "h4", "h5"]):
		heading_text = _node_text(heading).lower()
		if not any(pattern in heading_text for pattern in patterns):
			continue
		values: list[str] = []
		for node in heading.find_all_next():
			if node is heading:
				continue
			if node.name in {"h1", "h2", "h3", "h4", "h5"}:
				break
			if node.name == "li":
				values.append(_node_text(node, 2_000))
			elif node.name in {"p", "div"} and not node.find_parent("li"):
				text = _node_text(node, 2_000)
				if text:
					values.append(text)
		return list(dict.fromkeys(values))[:30]
	return []


def _description_text(soup: BeautifulSoup) -> str:
	"""Extract description prose without dumping section lists and headings."""
	for selector in ["[class*='description' i]", "meta[name='description']", "article", "main"]:
		node = soup.select_one(selector)
		if not node:
			continue
		if node.name == "meta":
			return _meta_or_node_text(node)
		copy = BeautifulSoup(str(node), "html.parser")
		for child in copy.find_all(["h1", "h2", "h3", "h4", "h5", "ul", "ol"]):
			child.decompose()
		text = _node_text(copy)
		if text:
			return text
	return ""


def _extract_job(soup: BeautifulSoup, original_url: str) -> dict[str, Any]:
	"""Extract a deterministic normalized job object from HTML and JSON-LD."""
	json_ld = _json_ld_job_posting(soup)
	_clean_document(soup)
	job_location = json_ld.get("jobLocation")
	organization = json_ld.get("hiringOrganization")

	title = _json_ld_text(json_ld.get("title")) or _first_text(soup, ["h1", "meta[property='og:title']", "title"]) or None
	company = _json_ld_text(organization) or _first_text(
		soup,
		["[data-testid*='company' i]", "[class*='company' i]", "meta[property='og:site_name']"],
	) or None
	location = _json_ld_text(job_location) or _first_text(soup, ["[data-testid*='location' i]", "[class*='location' i]"]) or None
	employment_type = _json_ld_text(json_ld.get("employmentType")) or _first_text(
		soup, ["[data-testid*='employment' i]", "[class*='employment' i]"]
	) or None
	description = _json_ld_text(json_ld.get("description")) or _description_text(soup) or None
	responsibilities = _json_ld_list(json_ld.get("responsibilities")) or _section_text(
		soup, ("responsibilit", "what you'll do", "what you will do", "duties")
	)
	required = _json_ld_list(json_ld.get("qualifications") or json_ld.get("educationRequirements")) or _section_text(
		soup, ("required qualification", "basic qualification", "requirements", "what we're looking for")
	)
	preferred = _json_ld_list(json_ld.get("preferredQualifications")) or _section_text(
		soup, ("preferred qualification", "preferred", "nice to have", "desired qualification")
	)
	skills = _json_ld_list(json_ld.get("skills")) or _section_text(soup, ("technical skill", "skills"))

	if not title and not description and not any((responsibilities, required, preferred, skills)):
		raise JobPostingError("insufficient_information", "The page does not contain enough job information.")
	return {
		"url": original_url,
		"title": title,
		"company": company,
		"location": location,
		"employment_type": employment_type,
		"description": description,
		"responsibilities": responsibilities,
		"required_qualifications": required,
		"preferred_qualifications": preferred,
		"skills": skills,
	}


@mcp.tool(
	structured_output=True,
	description=(
		"Retrieve a publicly accessible HTTP(S) job-posting page and return a "
		"concise structured extraction. Only public pages are supported; this "
		"tool does not log in, bypass CAPTCHA, scrape LinkedIn, or apply for jobs."
	),
)
async def analyze_job_posting(url: str) -> dict[str, Any]:
	"""Validate, fetch, and extract a public job posting from ``url``."""
	try:
		validated_url = _validate_url(url)
		html = await _fetch_html(validated_url)
		return _extract_job(BeautifulSoup(html, "html.parser"), validated_url)
	except JobPostingError as error:
		return {
			"ok": False,
			"error": {"code": error.code, "message": error.message},
			"original_url": url if isinstance(url, str) else "",
		}


@mcp.tool(
	structured_output=True,
	description=(
		"Retrieve explicit candidate evidence relevant to each requirement in a "
		"structured job object. This deterministic tool does not score, rank, "
		"recommend, infer qualifications, or generate prose."
	),
)
def match_candidate_to_job(job: dict[str, Any]) -> dict[str, Any]:
	"""Match structured job requirements to unchanged candidate evidence."""
	try:
		return match_candidate_to_job_data(job, _load_candidate_profile())
	except CandidateMatchError as error:
		return {"ok": False, "error": {"code": error.code, "message": error.message}}
	except CandidateProfileError as error:
		return {"ok": False, "error": {"code": error.code, "message": error.message}}


@mcp.tool(
	structured_output=True,
	description=(
		"Search PubMed through the NCBI E-utilities API and return bounded citation "
		"and abstract records. This tool retrieves and structures literature only; "
		"it does not summarize, rank, interpret, or assess relevance."
	),
)
async def search_pubmed(query: str, max_results: int) -> dict[str, Any]:
	"""Retrieve up to ten PubMed citation and abstract records for a query."""
	try:
		return await search_pubmed_data(query, max_results)
	except PubMedError as error:
		return {"ok": False, "error": {"code": error.code, "message": error.message}}

@mcp.prompt(
	name="cover_letter",
	title="Draft a tailored cover letter",
	description=(
		"Guide the model through analyzing a job posting, retrieving the candidate's "
		"source-of-truth evidence, matching evidence to the role, and drafting a "
		"grounded cover letter without inventing qualifications or experience."
	),
)
def cover_letter(job_url: str, additional_context: str | None = None) -> list[dict[str, Any]]:
	"""Return instructions for the LLM-led cover-letter workflow."""
	context = additional_context.strip() if isinstance(additional_context, str) else ""
	context_block = (
		f"\nAdditional user context (use only as context, not as evidence):\n{context}\n"
		if context
		else ""
	)
	return [
		{
			"role": "user",
			"content": {
				"type": "text",
				"text": f"""
You are preparing a cover letter only for the public job posting at:
{job_url}
{context_block}
Follow this evidence-grounded workflow exactly:

1. Call the MCP tool `analyze_job_posting` with the job URL above. Treat its
   structured result as the source of truth for the title, company, location,
   employment type, responsibilities, qualifications, and skills. Do not invent
   requirements that are absent from that result.
2. Call `match_candidate_to_job` with the structured job object returned by
	`analyze_job_posting`. This tool loads the local candidate profile internally
	and returns the candidate's source-of-truth evidence. Use its strong
	evidence, partial evidence, and gaps to choose relevant documented
	experiences. Do not calculate an overall score or make a hiring
	recommendation.
3. Draft a tailored professional cover letter using the 2-4 most relevant
   pieces of evidence. Prioritize direct professional experience, relevant
   technical or project experience, quantified accomplishments, education or
   certifications, and then other transferable evidence. Explain the connection
   to the role instead of copying the resume. Mention the company and position
   naturally, but do not fabricate familiarity with the company.

Grounding and output rules:
- Do not call `search_pubmed`; this is a cover-letter-only workflow.
- Do not perform interview preparation or other workflows.
- Do not bypass URL validation or access arbitrary local files.
- Do not claim an absent qualification. For gaps, use only an adjacent,
  explicitly documented experience when genuinely relevant.
- Do not mention evidence IDs in the final letter.
- Do not output the entire candidate profile, full job posting, raw matching
  result, or a numerical fit score.

Final response format:
1. Briefly identify the targeted role and company.
2. Provide the completed cover letter, ready for the candidate to use.
""".strip(),
			},
		}
	]


@mcp.prompt(
	name="interview_prep",
	title="Prepare for a job interview",
	description=(
		"Guide the model through job analysis, candidate evidence matching, focused "
		"PubMed retrieval, and grounded interview preparation."
	),
)
def interview_prep(job_url: str, additional_context: str | None = None) -> list[dict[str, Any]]:
	"""Return instructions for the LLM-led interview-preparation workflow."""
	context = additional_context.strip() if isinstance(additional_context, str) else ""
	context_block = (
		f"\nAdditional context (use to tailor preparation, but never as job or candidate evidence):\n{context}\n"
		if context
		else ""
	)
	return [
		{
			"role": "user",
			"content": {
				"type": "text",
				"text": f"""
You are preparing the candidate for an interview, not writing a cover letter, for:
{job_url}
{context_block}
Follow this workflow exactly:

1. Call `analyze_job_posting` with the job URL. Treat its structured result as
	the source of truth for the title, company, location, employment type,
	description, responsibilities, qualifications, and skills. Identify the
	role's major technical, scientific, product, clinical, regulatory, and
	cross-functional themes without inventing requirements.
2. Read the `candidate://profile` resource when it is available, treating it
	as read-only ground-truth candidate evidence. Then call
	`match_candidate_to_job` with the complete structured job object. The
	matcher also loads the local profile internally. Use its strong evidence,
	partial evidence, and explicit gaps; do not calculate a new score, rank the
	candidate, or make a hiring recommendation.
3. Identify 2-4 substantive biomedical, scientific, or technical topics from
	the actual role for which current literature would improve preparation.
	Do not search PubMed for generic topics such as leadership, communication,
	teamwork, interview skills, or product management alone.
4. Formulate one concise PubMed query for each selected topic automatically.
	Do not ask the user to provide queries. Call `search_pubmed` for each query
	with a conservative `max_results` of 3-5, and do not make dozens of searches.
	If a query returns no results, reformulate it at most once; then continue.
5. Synthesize the retrieved citation and abstract metadata. For each topic,
	explain why it matters to this role, what the candidate should know, and
	identify 1-3 relevant papers with title, useful authors, publication date,
	journal, DOI when available, PubMed URL, and a cautious explanation of
	relevance. Distinguish abstract-supported facts, reasonable inferences,
	and claims requiring full-text review. Never fabricate findings or sources.
6. Connect each topic to relevant matched candidate evidence when appropriate.
	Clearly distinguish direct experience, adjacent knowledge, newly researched
	information, and LLM-generated preparation guidance. Do not expose evidence
	IDs unless needed for internal traceability or claim work the candidate did
	not perform.

Generate questions grounded in the actual posting and profile, adjusting the
categories to the role:
- 4-6 technical or scientific questions
- 4-6 experience-based questions
- 2-4 product or business questions when relevant
- 2-4 behavioral questions when relevant
- 2-3 gap or challenge questions
For the highest-priority questions, provide talking points rather than
fabricated answers. Use Situation, Task, Action, Result where appropriate;
for technical questions provide the key concept, relevant evidence, what to
understand, and likely follow-ups.

Return an interview-preparation document with these sections when relevant:
1. What this role is really looking for
2. Your strongest evidence
3. Technical & scientific preparation, organized by topic
4. Likely interview questions by category
5. Answer preparation
6. Questions to ask the interviewer
7. Final preparation checklist

Workflow and safety rules:
- This is interview preparation only. Do not invoke the `cover_letter` prompt,
  generate a cover letter, or perform a separate workflow.
- Use `analyze_job_posting` for the URL; never bypass its validation or access
  arbitrary local files. Treat candidate data as read-only and never expose
  secrets.
- Do not perform general company web research. Use only the posting and the
  available PubMed results; do not imply company research that was not done.
- If PubMed is unavailable, or an individual search fails, continue with job
  analysis, candidate evidence, and interview questions. State clearly that
  literature retrieval was unavailable and do not fabricate citations.
- Do not call PubMed for generic interview or behavioral topics. Do not score,
  rank, recommend, or claim that newly researched knowledge is prior experience.
""".strip(),
			},
		}
	]


if __name__ == "__main__":
	mcp.run()
