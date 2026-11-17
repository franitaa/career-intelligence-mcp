# Career Intelligence MCP

This project explores an MCP server for structured career and research preparation, more specifically it helps the user draft a cover letter or prepare for an interview by matching a job posting's URL information to candidate experience.

Student name: Francesca Rondinella

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

### 1. Clone the repository

```bash
git clone https://github.com/franitaa/career-intelligence-mcp.git
cd career-intelligence-mcp
```

### 2. Install `uv`

On macOS with Homebrew:

```bash
brew install uv
```

Verify that it is available:

```bash
uv --version
```

### 3. Install dependencies

From the repository directory, run:

```bash
uv sync
```

`uv sync` creates or updates the project environment from `pyproject.toml` and
the locked dependency graph in `uv.lock`. The direct dependencies are also
listed in `requirements.txt` for reference.

### 4. Run the tests

```bash
uv run pytest
```

All tests should pass before connecting the server to an MCP client.

### 5. Configure Claude Desktop

Claude Desktop reads its MCP configuration from:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

Create the directory and open the configuration file if needed:

```bash
mkdir -p "$HOME/Library/Application Support/Claude"
open -a TextEdit "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
```

Add this configuration. Replace the `--directory` path if the repository is
stored somewhere else:

```json
{
	"mcpServers": {
		"career-intelligence": {
			"command": "/opt/homebrew/bin/uv",
			"args": [
				"run",
				"--directory",
				"/Users/francesca/Projects/career-intelligence-mcp",
				"python",
				"server.py"
			]
		}
	}
}
```

For Apple Silicon Macs, Homebrew normally installs `uv` at
`/opt/homebrew/bin/uv`. Confirm the path with `command -v uv` and update the
`command` value if necessary. The repository's
`claude_desktop_config.json` is a project-local example; Claude Desktop reads
the configuration file in its Application Support directory.

Validate the JSON configuration:

```bash
python3 -m json.tool "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
```

### 6. Restart Claude Desktop

Fully quit Claude Desktop with `Cmd+Q`, then reopen it. Enable the
`career-intelligence` connector if Claude asks for permission. The server runs
over MCP stdio; no web server or separate port is required.

### 7. Test the server

Use a public job-posting URL that does not require login, CAPTCHA, or browser
automation. In a new Claude Desktop chat, ask:

```text
Use the career-intelligence MCP tools to analyze this public job posting:
https://example.com/job-posting
```

For interview preparation, ask Claude to call `analyze_job_posting`, then
`match_candidate_to_job`, formulate focused PubMed queries, and call
`search_pubmed`. For a cover letter, ask it to call the same first two tools
and use only the returned candidate evidence. The `cover_letter` and
`interview_prep` workflows are separate.

To run the server directly for an MCP host or local debugging:

```bash
uv run python server.py
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
