"""Deterministic PubMed retrieval through the NCBI E-utilities API."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any

import httpx

NCBI_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# E-utilities endpoint and request parameters follow NCBI's official guidance:
# https://www.ncbi.nlm.nih.gov/books/NBK25501/
NCBI_API_KEY_ENV = "NCBI_API_KEY"
NCBI_EMAIL_ENV = "NCBI_EMAIL"
NCBI_TOOL_NAME_ENV = "NCBI_TOOL_NAME"
PUBMED_TIMEOUT_SECONDS = 10.0
MAX_QUERY_LENGTH = 500
MAX_RESPONSE_BYTES = 2_000_000
MAX_RESULTS = 10


class PubMedError(Exception):
    """An expected, safe PubMed application error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_inputs(query: Any, max_results: Any) -> tuple[str, int]:
    if not isinstance(query, str):
        raise PubMedError("invalid_query", "The PubMed query must be a string.")
    query = query.strip()
    if not query:
        raise PubMedError("invalid_query", "The PubMed query must not be empty.")
    if len(query) > MAX_QUERY_LENGTH:
        raise PubMedError("invalid_query", "The PubMed query is longer than the allowed limit.")
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise PubMedError("invalid_max_results", "max_results must be an integer from 1 to 10.")
    if not 1 <= max_results <= MAX_RESULTS:
        raise PubMedError("invalid_max_results", "max_results must be an integer from 1 to 10.")
    return query, max_results


def _request_params() -> dict[str, str]:
    params: dict[str, str] = {}
    for env_name, parameter in (
        (NCBI_API_KEY_ENV, "api_key"),
        (NCBI_EMAIL_ENV, "email"),
        (NCBI_TOOL_NAME_ENV, "tool"),
    ):
        value = os.getenv(env_name)
        if value:
            params[parameter] = value
    return params


async def _get_ncbi(client: httpx.AsyncClient, endpoint: str, params: Mapping[str, str]) -> bytes:
    try:
        async with client.stream("GET", f"{NCBI_BASE_URL}/{endpoint}", params=dict(params)) as response:
            if response.status_code == 429:
                raise PubMedError("rate_limited", "PubMed rate-limited the request. Please try again later.")
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise PubMedError("response_too_large", "The PubMed response exceeded the allowed size.")
    except PubMedError:
        raise
    except httpx.TimeoutException:
        raise PubMedError("timeout", "The PubMed request took too long to respond.") from None
    except httpx.HTTPStatusError as error:
        raise PubMedError("http_error", f"PubMed returned HTTP {error.response.status_code}.") from None
    except httpx.RequestError:
        raise PubMedError("unreachable", "PubMed could not be reached.") from None

    return bytes(content)


def _parse_search_ids(payload: bytes) -> list[str]:
    try:
        data = json.loads(payload)
        ids = data["esearchresult"]["idlist"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise PubMedError("invalid_response", "PubMed returned an unexpected search response.") from None
    if not isinstance(ids, list) or not all(isinstance(pmid, str) and pmid for pmid in ids):
        raise PubMedError("invalid_response", "PubMed returned an unexpected search response.")
    return ids


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = "".join(element.itertext()).strip()
    return " ".join(value.split()) or None


def _publication_date(article: ET.Element) -> str | None:
    date = article.find(".//ArticleDate")
    if date is not None:
        parts = [date.findtext(name) for name in ("Year", "Month", "Day")]
        return "-".join(part for part in parts if part) or None
    pub_date = article.find(".//JournalIssue/PubDate")
    if pub_date is None:
        return None
    parts = [pub_date.findtext(name) for name in ("Year", "Month", "Day")]
    structured = "-".join(part for part in parts if part)
    return structured or _text(pub_date)


def _authors(article: ET.Element) -> list[str]:
    authors: list[str] = []
    for author in article.findall(".//AuthorList/Author"):
        collective = _text(author.find("CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        name = " ".join(
            part for part in (
                _text(author.find("ForeName")),
                _text(author.find("LastName")),
            ) if part
        )
        if name:
            authors.append(name)
    return authors


def _abstract(article: ET.Element) -> str | None:
    sections: list[str] = []
    for abstract in article.findall(".//Abstract/AbstractText"):
        text = _text(abstract)
        if not text:
            continue
        label = abstract.attrib.get("Label")
        sections.append(f"{label}: {text}" if label else text)
    return " ".join(sections) or None


def _doi(article: ET.Element) -> str | None:
    for identifier in article.findall(".//ArticleIdList/ArticleId"):
        if identifier.attrib.get("IdType") == "doi":
            return _text(identifier)
    return None


def _parse_articles(payload: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise PubMedError("invalid_response", "PubMed returned malformed article data.") from None

    results: list[dict[str, Any]] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = _text(article.find(".//PMID"))
        title = _text(article.find(".//ArticleTitle"))
        if not pmid or not title:
            continue
        journal = _text(article.find(".//Journal/Title"))
        results.append(
            {
                "pmid": pmid,
                "title": title,
                "authors": _authors(article),
                "journal": journal,
                "publication_date": _publication_date(article),
                "doi": _doi(article),
                "abstract": _abstract(article),
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            }
        )
    return results


def _error_result(error: PubMedError) -> dict[str, Any]:
    return {"ok": False, "error": {"code": error.code, "message": error.message}}


async def search_pubmed_data(query: Any, max_results: Any) -> dict[str, Any]:
    """Search PubMed and return bounded citation/abstract records."""
    query, max_results = _validate_inputs(query, max_results)
    common_params = {"db": "pubmed", "retmode": "json", **_request_params()}
    search_params = {**common_params, "term": query, "retmax": str(max_results)}

    async with httpx.AsyncClient(timeout=httpx.Timeout(PUBMED_TIMEOUT_SECONDS)) as client:
        ids = _parse_search_ids(await _get_ncbi(client, "esearch.fcgi", search_params))
        if not ids:
            return {"query": query, "source": "PubMed", "results": []}
        fetch_params = {
            "db": "pubmed",
            "id": ",".join(ids[:max_results]),
            "retmode": "xml",
            **_request_params(),
        }
        records = _parse_articles(await _get_ncbi(client, "efetch.fcgi", fetch_params))
    return {"query": query, "source": "PubMed", "results": records[:max_results]}
