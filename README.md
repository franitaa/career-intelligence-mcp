# Career Intelligence MCP

Project scaffold for a university assignment that will explore an MCP server
for structured career and research preparation.

## Assignment Deliverables

| Deliverable | Implementation |
| --- | --- |
| GitHub link | **Replace this placeholder with the repository URL:** `https://github.com/<your-username>/career-intelligence-mcp` |
| Domain choice | Career intelligence for biomedical and technical job-search preparation, including job extraction, evidence-grounded matching, cover letters, interview preparation, and biomedical literature retrieval. |
| Best practice 1 | **Separation of retrieval and reasoning:** Python validates and retrieves structured data; Claude performs synthesis and writing. |
| Best practice 2 | **Security and bounded access:** public HTTP(S)-only job retrieval, SSRF protection, response/time limits, read-only candidate evidence, bounded PubMed requests, and model-readable errors. |

The repository currently has no configured Git remote, so the GitHub URL above
must be replaced with the actual repository link before submission.

## Reflection

### What challenges did you encounter?

The main challenges were designing reliable extraction from inconsistent job
posting HTML, preventing server-side URL fetching from reaching private network
destinations, and keeping generated career claims grounded in explicit
candidate evidence. PubMed also introduces external failures such as timeouts,
rate limits, malformed responses, and empty searches. The solution was to use
bounded requests, validate every redirect, return structured error objects, and
keep literature retrieval separate from the LLM's interpretation. A practical
integration challenge was that some MCP clients expose tools but not resources
or prompts, so the matching tool also loads the local profile internally while
the profile remains available as a read-only MCP resource.

### How did you choose the level of abstraction?

The server is organized around user-meaningful operations and clear ownership
boundaries. `analyze_job_posting` is one tool because URL validation, fetching,
HTML parsing, and structured extraction form one retrieval operation.
`match_candidate_to_job` is separate because deterministic evidence matching is
independently testable and should not be mixed with extraction or LLM prose.
`search_pubmed` is another tool because it has a different external API,
validation rules, failure modes, and attribution requirements. The
`cover_letter` and `interview_prep` prompts orchestrate these tools without
duplicating their logic: Python retrieves and validates data, while Claude
chooses evidence, reasons about relevance, and writes the final preparation or
letter.

### What are the limitations, and how would you improve it?

The job parser works best on public, server-rendered pages and may miss content
that requires JavaScript, authentication, CAPTCHA handling, or unusual page
layouts. The matcher uses transparent keyword and tag matching, so it does not
understand synonyms or nuanced experience equivalence. PubMed provides
citations and abstracts rather than full-text evidence, and the server does not
provide general company research. The current system also depends on an MCP
client exposing the relevant tools and, where available, resources and prompts.
Future improvements could add carefully tested site-specific extraction
adapters, a reviewed synonym vocabulary for matching, stronger caching and
rate-limit handling, and an optional general web-search capability with the
same security and attribution controls. Those additions would require new
scope and should not weaken the current evidence-grounding boundaries.

## Current status

The first read-only MCP tool is implemented:

`analyze_job_posting(url: str)` validates a public HTTP(S) URL, fetches a
bounded HTML response, and returns a concise structured extraction containing
the title, company, location, description, responsibilities, qualifications,
technical/domain skills, and original URL.

It deliberately does not log in, bypass CAPTCHA, use browser automation, scrape
LinkedIn, apply for jobs, or send messages. The server returns model-readable
error objects for invalid URLs, blocked destinations, HTTP failures, timeouts,
oversized responses, and pages with insufficient information.

The server also exposes the read-only `candidate://profile` resource from
`data/candidate_profile.json`, and the deterministic
`match_candidate_to_job(job)` tool for retrieving explicit candidate evidence
by job requirement. Matching does not score, rank, or infer qualifications.

### `cover_letter` prompt

The `cover_letter` MCP prompt accepts a required `job_url` and an optional
`additional_context`. It guides an LLM client through this cover-letter-only
workflow:

1. Call `analyze_job_posting(job_url)` for structured job facts.
2. Call `match_candidate_to_job(job)` to retrieve strong evidence, partial
	evidence, and explicit gaps. The matcher loads the local candidate profile
	 internally.
3. Draft a tailored letter using only documented candidate evidence.

The prompt does not call `search_pubmed`, perform interview preparation, score
the candidate, or generate the letter in Python. Claude performs the final
reasoning and writing while preserving the separation between retrieval,
matching, and synthesis. Claims must remain grounded in the local candidate
profile loaded by the matcher.

Conceptual workflow boundaries:

```text
			    Career Intelligence MCP
				      |
		   +----------------+----------------+
		   |                                 |
	   cover_letter                       interview_prep
		   |                                 |
		   v                                 v
	analyze_job_posting                 analyze_job_posting
		   |                                 |
		   v                                 v
	match_candidate_to_job             match_candidate_to_job
		   |                                 |
		   v                                 v
	   Tailored letter                 Generate 2-4 topics
							  |
							  v
						 search_pubmed (3-5 each)
							  |
							  v
						      Synthesis
```

### `interview_prep` prompt

The `interview_prep` MCP prompt accepts a required `job_url` and optional
`additional_context`. Claude uses the existing tools and resource to:

1. Analyze the public job posting and identify its actual role themes.
2. Read `candidate://profile` when supported by the MCP client and call
	`match_candidate_to_job(job)` for grounded evidence and explicit gaps.
3. Select 2-4 substantive biomedical, scientific, or technical topics from the
	role, excluding generic interview and behavioral topics.
4. Formulate focused PubMed queries automatically and call
	`search_pubmed(query, max_results)` with a conservative 3-5 results per
	query.
5. Synthesize abstract-supported literature, candidate evidence, interview
	questions, talking points, interviewer questions, and a preparation checklist.

PubMed failures do not stop interview preparation. Claude continues with the
job analysis, candidate matching, and interview questions while clearly
reporting that literature retrieval was unavailable. This workflow does not
perform general company web research, calculate a candidate score, make a
hiring recommendation, or generate a cover letter. The `cover_letter` prompt
remains a separate workflow and does not call PubMed.

### `search_pubmed(query, max_results)`

This tool searches biomedical literature through the official NCBI E-utilities
API using the PubMed database. It performs an ESearch followed by EFetch and
returns citation and abstract information only: PMID, title, authors, journal,
publication date, DOI, abstract, and direct PubMed URL. It does not retrieve
full-text articles, summarize papers, rank relevance, or interpret findings.

`query` must be a non-empty string of at most 500 characters. `max_results`
must be an integer from 1 through 10. Requests are sequential and bounded,
with a 10-second timeout and a 2 MB response limit.

Optional NCBI request identification variables are supported:

- `NCBI_API_KEY`: an NCBI API key, if available
- `NCBI_EMAIL`: contact email for NCBI requests
- `NCBI_TOOL_NAME`: tool name identifying this client

The tool is intended for low-volume assignment use. Follow NCBI E-utilities
usage guidance and rate limits, handle HTTP 429 responses, and preserve PubMed
and NCBI attribution in downstream work:

- https://www.ncbi.nlm.nih.gov/books/NBK25501/
- https://www.ncbi.nlm.nih.gov/books/NBK25497/

## Setup

```bash
cd /Users/francesca/Projects/career-intelligence-mcp
source .venv/bin/activate
uv sync
```

The direct dependencies are listed in `requirements.txt`; `uv.lock` records the
complete resolved dependency graph.

Run the server over stdio for an MCP host:

```bash
uv run python server.py
```

The included `claude_desktop_config.json` points Claude Desktop at this project.

Run the tests with:

```bash
uv run pytest
```

## Planned scope

The eventual server may retrieve and structure job postings, company and
scientific research information, candidate profile data, and relationship data
for use by an LLM. Claude should perform synthesis and reasoning. The server
will validate inputs, retrieve data, and return structured results. It will not
submit applications, send messages, or require LinkedIn scraping for the MVP.

## Implementation notes

The server uses the official MCP Python SDK v2 API (`mcp==2.2.0`) with
`MCPServer` and `@mcp.tool`, not the deprecated v1 `FastMCP` API. Tests use the
SDK's in-process `Client(mcp)` pattern, so they do not need a subprocess, port,
or real external web request.

Future tools may use retrieved literature for LLM-led synthesis, interview
preparation, or research context. Those reasoning tasks remain outside this
retrieval tool.
