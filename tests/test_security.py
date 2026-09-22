"""Security tests for URL validation and bounded HTTP retrieval."""

import httpx
import pytest

import server


def test_localhost_url_is_blocked() -> None:
	with pytest.raises(server.JobPostingError, match="Local destinations"):
		server._validate_url("http://localhost/job")


def test_loopback_and_private_ip_urls_are_blocked() -> None:
	for address in ("127.0.0.1", "[::1]", "10.0.0.5", "192.168.1.20", "169.254.1.1"):
		with pytest.raises(server.JobPostingError, match="Private or local"):
			server._validate_url(f"http://{address}/job")


def test_hostname_resolving_to_private_ip_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(
		server.socket,
		"getaddrinfo",
		lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 80))],
	)
	with pytest.raises(server.JobPostingError, match="Private or local"):
		server._validate_url("https://public-looking.example/job")


def test_excessively_long_url_is_rejected() -> None:
	with pytest.raises(server.JobPostingError, match="longer than"):
		server._validate_url("https://example.com/" + "a" * server.MAX_URL_LENGTH)


class _FakeResponse:
	status_code = 200
	headers: dict[str, str] = {}
	encoding = "utf-8"

	def raise_for_status(self) -> None:
		return None

	async def __aenter__(self) -> "_FakeResponse":
		return self

	async def __aexit__(self, *args: object) -> None:
		return None


class _FakeClient:
	def __init__(self, *, chunks: list[bytes] | None = None, timeout: bool = False) -> None:
		self.chunks = chunks or []
		self.timeout = timeout

	async def __aenter__(self) -> "_FakeClient":
		return self

	async def __aexit__(self, *args: object) -> None:
		return None

	def stream(self, method: str, url: str) -> _FakeResponse:
		response = _FakeResponse()

		async def aiter_bytes():
			if self.timeout:
				raise httpx.ReadTimeout("simulated timeout")
			for chunk in self.chunks:
				yield chunk

		response.aiter_bytes = aiter_bytes  # type: ignore[attr-defined]
		return response


@pytest.mark.anyio
async def test_timeout_is_returned_as_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(server.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 80))])
	monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: _FakeClient(timeout=True))
	with pytest.raises(server.JobPostingError, match="too long"):
		await server._fetch_html("https://example.com/job")


@pytest.mark.anyio
async def test_response_size_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(server.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 80))])
	chunks = [b"x" * (server.MAX_RESPONSE_BYTES + 1)]
	monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: _FakeClient(chunks=chunks))
	with pytest.raises(server.JobPostingError, match="download limit"):
		await server._fetch_html("https://example.com/job")


@pytest.mark.anyio
async def test_redirect_to_private_ip_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
	class RedirectResponse(_FakeResponse):
		status_code = 302
		headers = {"location": "http://127.0.0.1/private"}

	class RedirectClient(_FakeClient):
		def stream(self, method: str, url: str) -> RedirectResponse:
			return RedirectResponse()

	monkeypatch.setattr(server.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 80))])
	monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: RedirectClient())
	with pytest.raises(server.JobPostingError, match="Private or local"):
		await server._fetch_html("https://example.com/job")
