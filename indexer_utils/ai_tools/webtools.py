"""Web research tools for the discovery subagents: Brave Search + page fetch.

Replaces the OpenAI-hosted ``WebSearchTool`` (only works against
api.openai.com, so it can't follow us to another provider). ``brave_search``
hits the Brave Search API directly; ``web_fetch`` downloads a page either
statically (httpx) or through a headless Chromium (``render=True``) for
sites that only produce their body in JavaScript (Reddit, some chart
pages). Both return plain text sized for an LLM reader — the subagents
consume the output directly, so the tools do the extraction and capping
here rather than dumping raw HTML at the model.
"""

import asyncio
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlsplit

from agents import RunContextWrapper
from decouple import config
from httpx import AsyncClient

from .safe_tool import safe_tool

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
_FETCH_TIMEOUT = 20.0  # seconds, static + API
_RENDER_TIMEOUT_MS = 30_000
# A single page the model should never need more of; the dossier subagent
# caps its own output, and every fetched page accumulates in the model's
# context — 8k chars (≈2-3k tokens) keeps even a 12-fetch research run far
# under the 92k context ceiling of the local model.
_MAX_CONTENT_CHARS = 8_000
_MAX_REDIRECTS = 5

# Rendered fetches run a real Chromium — by far the heaviest path here. Cap
# concurrent renders per event loop (two) so a burst of subagent calls
# doesn't stack heavy browsers while the rest of the pipeline runs.
# Keyed per loop on purpose: scheduler jobs spin up short-lived
# asyncio.run loops (see agent.py), and an asyncio.Semaphore binds to the
# first loop that awaits it — a single module-level instance would raise
# on the second loop.
_render_semaphores: Dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _render_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    for closed in [lp for lp in _render_semaphores if lp.is_closed()]:
        del _render_semaphores[closed]
    sem = _render_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(2)
        _render_semaphores[loop] = sem
    return sem


class _TextExtractor(HTMLParser):
    """HTML -> readable plain text: drop scripts/styles, newline on blocks."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "head"}
    _BLOCK = {
        "p",
        "div",
        "li",
        "tr",
        "td",
        "th",
        "ul",
        "ol",
        "table",
        "section",
        "article",
        "br",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "dd",
        "dt",
        "figcaption",
        "header",
        "footer",
        "main",
        "nav",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def _extract(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return [str(info[4][0]) for info in infos]


async def _blocked(url: str) -> Optional[str]:
    """Why ``url`` must not be fetched, or None when it's a public http(s) URL.

    The URL comes from the model, and the model reads pages that can carry
    injected instructions, while this process sits on the Docker network
    next to Radarr, Sonarr, Authelia, Redis and Postgres. So every target
    — and every redirect hop — must resolve only to globally routable
    addresses: no loopback, private, link-local (cloud metadata), shared or
    reserved ranges, and no bare service names that resolve to them.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "url must be an absolute http(s) URL"
    host = parts.hostname
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = await _resolve(host, port)
    except (OSError, ValueError):
        return f"could not resolve {host}"
    if not addresses:
        return f"could not resolve {host}"
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return f"refusing to fetch {host}: it resolves to a non-public address"
    return None


def _page_payload(url: str, text: str, **extra: Any) -> Dict[str, Any]:
    return {
        "url": url,
        "chars": len(text),
        "content": text[:_MAX_CONTENT_CHARS],
        **extra,
    }


@safe_tool
async def brave_search(
    wrapper: RunContextWrapper[Any],
    query: str,
    count: int = 10,
    freshness: str = "",
) -> Dict[str, Any]:
    """Search the web (Brave Search API). Returns a list of {title, url, snippet}.

    Use to find pages worth fetching: box-office charts, Nielsen coverage,
    review sites, Reddit threads, Wikipedia pages. Then pass the chosen URLs
    to web_fetch to read the actual content — snippets alone are rarely
    enough for a dossier.

    Args:
        query: Search query.
        count: Results to return (1-20, default 10).
        freshness: Optional recency filter — 'pd' (day), 'pw' (week), 'pm'
            (month), 'py' (year), or '' for no filter.
    """
    api_key = config("BRAVE_API_KEY", default="")
    if not api_key:
        return {"error": "BRAVE_API_KEY not configured"}

    query = (query or "").strip()
    if not query:
        return {"error": "query is required"}
    count = max(1, min(20, int(count or 10)))
    params: Dict[str, Any] = {"q": query, "count": count}
    if freshness in {"pd", "pw", "pm", "py"}:
        params["freshness"] = freshness

    async with AsyncClient(timeout=_FETCH_TIMEOUT) as client:
        resp = await client.get(
            _BRAVE_ENDPOINT,
            params=params,
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        )
    if resp.status_code != 200:
        return {"error": f"brave search HTTP {resp.status_code}"}

    results = [
        {
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": item.get("description") or "",
        }
        for item in (resp.json().get("web") or {}).get("results") or []
    ]
    return {"query": query, "results": results[:count]}


@safe_tool
async def web_fetch(
    wrapper: RunContextWrapper[Any],
    url: str,
    render: bool = False,
) -> Dict[str, Any]:
    """Fetch a web page and return its readable text (scripts and markup stripped).

    Default is a fast static fetch. Some sites (Reddit, IMDb, interactive
    charts) only produce their body via JavaScript — a static fetch of those
    returns a near-empty shell; when that happens, retry the same URL with
    render=true, which runs it through a headless browser. Slower, so don't
    render by default.

    Args:
        url: Absolute http(s) URL.
        render: True to run the page in a headless browser (JavaScript
            rendering). Use only when a static fetch came back empty.
    """
    url = (url or "").strip()
    if reason := await _blocked(url):
        return {"error": reason}

    if render:
        return await _fetch_rendered(url)

    # Redirects are followed by hand so every hop passes the same check.
    async with AsyncClient(
        timeout=_FETCH_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        target = url
        for _ in range(_MAX_REDIRECTS + 1):
            resp = await client.get(target)
            location = resp.headers.get("location")
            if not (resp.is_redirect and location):
                break
            target = urljoin(target, location)
            if reason := await _blocked(target):
                return {"error": f"redirect blocked: {reason}"}
        else:
            return {"error": f"too many redirects for {url}"}
    if resp.status_code >= 400:
        return {"error": f"HTTP {resp.status_code} for {url}"}
    return _page_payload(url, _extract(resp.text))


async def _fetch_rendered(url: str) -> Dict[str, Any]:
    async with _render_semaphore():
        # Imported here so a machine/image without the browser installed
        # still gets static fetches instead of an import error at module load.
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(user_agent=_USER_AGENT)
                verdicts: Dict[str, Optional[str]] = {}

                async def guard(route: Any) -> None:
                    """Abort any request the page makes to a non-public host."""
                    parts = urlsplit(route.request.url)
                    key = f"{parts.scheme}://{parts.netloc}"
                    if key not in verdicts:
                        verdicts[key] = await _blocked(route.request.url)
                    if verdicts[key]:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", guard)
                response = await page.goto(
                    url, timeout=_RENDER_TIMEOUT_MS, wait_until="domcontentloaded"
                )
                # Route handlers only see the first URL of a redirect chain,
                # so check the navigation's hops after the fact and withhold
                # the page if any of them was internal.
                request = response.request if response else None
                while request is not None:
                    if reason := await _blocked(request.url):
                        return {"error": f"redirect blocked: {reason}"}
                    request = request.redirected_from
                # Give client-side hydration a beat before reading the DOM.
                await page.wait_for_timeout(1500)
                text = await page.inner_text("body")
            finally:
                await browser.close()
    return _page_payload(url, text, rendered=True)
