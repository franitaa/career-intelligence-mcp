"""Mocked tests for deterministic PubMed E-utilities retrieval."""

import httpx
import pytest
from mcp import Client

import pubmed_search
import server


SEARCH_RESPONSE = b'{"esearchresult":{"idlist":["12345678","87654321"]}}'
FETCH_RESPONSE = b"""
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <ArticleTitle>Microfluidic ultrasound contrast agents</ArticleTitle>
        <Journal><Title>Biomedical Journal</Title><JournalIssue><PubDate><Year>2025</Year><Month>Jan</Month></PubDate></JournalIssue></Journal>
        <AuthorList>
          <Author><ForeName>Ana</ForeName><LastName>Author</LastName></Author>
          <Author><CollectiveName>Research Group</CollectiveName></Author>
        </AuthorList>
        <Abstract>
          <AbstractText Label="BACKGROUND">Background text.</AbstractText>
          <AbstractText Label="METHODS">Methods text.</AbstractText>
        </Abstract>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList><ArticleId IdType="doi">10.1000/example</ArticleId></ArticleIdList></PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>87654321</PMID>
      <Article>
        <ArticleTitle>Record without optional metadata</ArticleTitle>
        <Journal><Title>Another Journal</Title><JournalIssue><PubDate><MedlineDate>Winter 2024</MedlineDate></PubDate></JournalIssue></Journal>
        <AuthorList><Author><LastName>OnlyLastName</LastName></Author></AuthorList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://eutils.ncbi.nlm.nih.gov")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("failure", request=request, response=response)

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aiter_bytes(self):
        yield self.content


class FakeClient:
    responses: list[FakeResponse] = []
    calls: list[tuple[str, dict[str, str]]] = []

    def __init__(self, **_: object) -> None:
        self.responses = type(self).responses.copy()
        self.calls = type(self).calls

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str, *, params: dict[str, str]) -> FakeResponse:
        self.calls.append((url, params))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.responses = []
    FakeClient.calls = []
    monkeypatch.setattr(pubmed_search.httpx, "AsyncClient", FakeClient)
    return FakeClient


def test_happy_path_extracts_citation_fields(fake_http: type[FakeClient]) -> None:
    fake_http.responses = [FakeResponse(SEARCH_RESPONSE), FakeResponse(FETCH_RESPONSE)]

    result = asyncio_run(pubmed_search.search_pubmed_data("microfluidic ultrasound contrast agents", 2))

    assert result["query"] == "microfluidic ultrasound contrast agents"
    assert result["source"] == "PubMed"
    assert result["results"][0] == {
        "pmid": "12345678",
        "title": "Microfluidic ultrasound contrast agents",
        "authors": ["Ana Author", "Research Group"],
        "journal": "Biomedical Journal",
        "publication_date": "2025-Jan",
        "doi": "10.1000/example",
        "abstract": "BACKGROUND: Background text. METHODS: Methods text.",
        "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
    }
    assert result["results"][1]["doi"] is None
    assert result["results"][1]["abstract"] is None
    assert result["results"][1]["publication_date"] == "Winter 2024"
    assert fake_http.calls[0][1]["db"] == "pubmed"
    assert fake_http.calls[1][1]["id"] == "12345678,87654321"


def asyncio_run(awaitable):
    """Run a coroutine in a test without adding an async test dependency."""
    import asyncio

    return asyncio.run(awaitable)


def test_empty_search_returns_empty_results(fake_http: type[FakeClient]) -> None:
    fake_http.responses = [FakeResponse(b'{"esearchresult":{"idlist":[]}}')]

    result = asyncio_run(pubmed_search.search_pubmed_data("rare biomedical topic", 3))

    assert result == {"query": "rare biomedical topic", "source": "PubMed", "results": []}
    assert len(fake_http.calls) == 1


@pytest.mark.parametrize("query", ["", "   ", 42, None])
def test_query_validation(query: object) -> None:
    with pytest.raises(pubmed_search.PubMedError, match="query"):
        asyncio_run(pubmed_search.search_pubmed_data(query, 1))


@pytest.mark.parametrize("value", [0, -1, 11, "2", True])
def test_max_results_validation(value: object) -> None:
    with pytest.raises(pubmed_search.PubMedError, match="max_results"):
        asyncio_run(pubmed_search.search_pubmed_data("cancer", value))


def test_long_query_validation() -> None:
    with pytest.raises(pubmed_search.PubMedError, match="longer"):
        asyncio_run(pubmed_search.search_pubmed_data("x" * (pubmed_search.MAX_QUERY_LENGTH + 1), 1))


def test_http_error_timeout_malformed_and_rate_limit(fake_http: type[FakeClient]) -> None:
    for response, expected in (
        (FakeResponse(b"", 500), "http_error"),
        (httpx.ReadTimeout("timeout"), "timeout"),
        (FakeResponse(b"", 429), "rate_limited"),
    ):
        fake_http.responses = [response]
        with pytest.raises(pubmed_search.PubMedError) as error:
            asyncio_run(pubmed_search.search_pubmed_data("cancer", 1))
        assert error.value.code == expected

    fake_http.responses = [FakeResponse(b"not json")]
    with pytest.raises(pubmed_search.PubMedError, match="unexpected search"):
        asyncio_run(pubmed_search.search_pubmed_data("cancer", 1))

    fake_http.responses = [FakeResponse(b'{"esearchresult":{"idlist":["1"]}}'), FakeResponse(b"<broken")]
    with pytest.raises(pubmed_search.PubMedError, match="malformed"):
        asyncio_run(pubmed_search.search_pubmed_data("cancer", 1))


def test_environment_secrets_are_used_but_never_returned(fake_http: type[FakeClient], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NCBI_API_KEY", "secret-api-key")
    monkeypatch.setenv("NCBI_EMAIL", "candidate@example.com")
    monkeypatch.setenv("NCBI_TOOL_NAME", "career-intelligence-test")
    fake_http.responses = [FakeResponse(b"not json")]

    with pytest.raises(pubmed_search.PubMedError) as error:
        asyncio_run(pubmed_search.search_pubmed_data("cancer", 1))
    assert "secret-api-key" not in str(error.value)
    assert fake_http.calls[0][1]["api_key"] == "secret-api-key"
    assert fake_http.calls[0][1]["email"] == "candidate@example.com"


@pytest.mark.anyio
async def test_mcp_tool_is_registered_and_callable(fake_http: type[FakeClient]) -> None:
    fake_http.responses = [FakeResponse(SEARCH_RESPONSE), FakeResponse(FETCH_RESPONSE)]
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        tool = next(tool for tool in tools.tools if tool.name == "search_pubmed")
        result = await client.call_tool("search_pubmed", {"query": "microfluidics", "max_results": 2})

    assert tool.input_schema["required"] == ["query", "max_results"]
    assert result.structured_content["source"] == "PubMed"
    assert result.structured_content["results"][0]["pmid"] == "12345678"
