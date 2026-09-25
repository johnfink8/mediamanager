"""web_fetch must never reach a non-public address, directly or by redirect."""

import socket

import httpx
import pytest

from indexer_utils.ai_tools import webtools as wt

PUBLIC = "93.184.215.14"


@pytest.fixture
def dns(monkeypatch):
    """Map hostnames to addresses; anything unmapped fails to resolve."""
    table = {}

    async def fake_resolve(host, port):
        if host not in table:
            raise socket.gaierror("not found")
        return [table[host]]

    monkeypatch.setattr(wt, "_resolve", fake_resolve)
    return table


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "172.18.0.4",  # docker bridge
        "192.168.1.244",  # LAN
        "169.254.169.254",  # link-local / cloud metadata
        "100.64.0.1",  # shared address space
        "::1",
        "fd00::1",
        "::ffff:10.0.0.5",  # IPv4-mapped private
    ],
)
async def test_non_public_addresses_are_blocked(dns, address):
    dns["radarr"] = address
    assert "non-public" in await wt._blocked("http://radarr:7878/api/v3/system")


async def test_public_address_is_allowed(dns):
    dns["example.com"] = PUBLIC
    assert await wt._blocked("https://example.com/page") is None


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "/rel"])
async def test_non_http_urls_are_blocked(dns, url):
    assert "http(s)" in await wt._blocked(url)


async def test_unresolvable_host_is_blocked(dns):
    assert "could not resolve" in await wt._blocked("http://nowhere.invalid/")


def _client_factory(monkeypatch, handler):
    real = httpx.AsyncClient

    def make(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(wt, "AsyncClient", make)


async def test_redirect_to_internal_host_is_not_followed(dns, monkeypatch):
    dns["example.com"] = PUBLIC
    dns["sonarr"] = "172.18.0.9"
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://sonarr:8989/api"})
        return httpx.Response(200, text="secret")

    _client_factory(monkeypatch, handler)
    out = await wt.web_fetch.__wrapped__(None, url="https://example.com/start")
    assert out["error"].startswith("redirect blocked")
    assert fetched == ["https://example.com/start"]


async def test_public_redirects_are_followed(dns, monkeypatch):
    dns["example.com"] = PUBLIC
    dns["www.example.com"] = PUBLIC

    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(
                301, headers={"location": "https://www.example.com/p"}
            )
        return httpx.Response(200, text="<p>hello</p>")

    _client_factory(monkeypatch, handler)
    out = await wt.web_fetch.__wrapped__(None, url="https://example.com/p")
    assert out["content"] == "hello"


async def test_redirect_loop_is_capped(dns, monkeypatch):
    dns["example.com"] = PUBLIC

    def handler(request):
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    _client_factory(monkeypatch, handler)
    out = await wt.web_fetch.__wrapped__(None, url="https://example.com/")
    assert "too many redirects" in out["error"]
