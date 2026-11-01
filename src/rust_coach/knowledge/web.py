import asyncio
import ipaddress
import json
import logging
import socket
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup


logger = logging.getLogger("rust_coach")
ALLOWED_HOSTS = {"rust.facepunch.com", "wiki.facepunch.com", "rust-data.com", "rusthelp.com",
                 "www.rustafied.com", "rustafied.com"}
TOPICS = {
    "building": ("Rust PC base building airlock doors soft side guide", "https://www.rustafied.com/building-in-rust/"),
    "upkeep": ("Rust PC tool cupboard upkeep group scaling decay", "https://wiki.facepunch.com/rust/the_tool_cupboard"),
    "progression": ("Rust PC workbench blueprint research tech tree progression update", "https://rust.facepunch.com/news/upgrade-hard-raid-harder"),
    "recycling": ("Rust PC recycler power grid efficiency update", "https://rust.facepunch.com/news/power-trip"),
    "monuments": ("Rust PC monument puzzle green keycard fuse guide", "https://www.rustafied.com/monument-puzzle-basic-green-solutions"),
    "combat": ("Rust PC equipment armor combat guide", "https://www.rustafied.com/rust-damage-armor-and-you-guide"),
    "updates": ("Rust PC latest update Facepunch devblog", "https://rust.facepunch.com/news"),
}
TOPIC_TERMS = {
    "building": ("airlock", "entrance", "foundation", "building", "door", "wall"),
    "upkeep": ("tool cupboard", "upkeep", "decay", "maintenance"),
    "progression": ("workbench", "blueprint", "tech tree", "research", "scrap"),
    "recycling": ("recycler", "recycling", "efficiency", "power grid"),
    "monuments": ("puzzle", "keycard", "fuse", "monument"),
    "combat": ("armor", "damage", "weapon", "projectile", "bleeding"),
    "updates": ("update", "patch", "changelog", "devblog", "changes"),
}


def specific_url(url):
    path = urlsplit(url).path.rstrip("/")
    return bool(path and path not in {"/play", "/buy", "/companion", "/skins", "/legal"})


def relevant(document, topics):
    text = (document["title"] + " " + document["text"]).lower()
    return specific_url(document["url"]) and any(
        sum(term in text for term in TOPIC_TERMS[topic]) >= 2 for topic in topics)


class RetrievalError(Exception):
    pass


def safe_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query):
        raise RetrievalError("source_not_allowed")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", "", ""))


async def public_dns(host):
    addresses = await asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, host, 443)
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise RetrievalError("non_public_address")


def extract_document(url, html):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True)[:250] if soup.title else url
    published = soup.select_one('meta[property="article:published_time"], meta[name="date"], time[datetime]')
    publication = (published.get("content") or published.get("datetime")) if published else None
    for element in soup.select("script, style, svg, nav, header, footer, aside, form, noscript, [hidden], [aria-hidden=true]"):
        element.decompose()
    main = soup.select_one("article") or soup.select_one("main") or soup.body or soup
    text = main.get_text(" ", strip=True)
    text = " ".join(text.split())
    for marker in ("Page views:", "Account Management Log In"):
        text = text.split(marker)[0]
    if len(text) < 180 or any(phrase in title.lower() for phrase in ("just a moment", "access denied", "not found")):
        raise RetrievalError("no_readable_body")
    return {"url": url, "title": title, "text": text[:18000], "fetched_at": datetime.now(timezone.utc).isoformat(),
            "published_at": publication, "game_build": None, "version_status": "unverified",
            "source_type": "official_update" if url.startswith("https://rust.facepunch.com/news/") else
                           "official" if urlsplit(url).hostname == "rust.facepunch.com" else
                           "wiki" if "wiki.facepunch.com" in url else "community",
            "truncated": len(text) > 18000}


class WebResearch:
    def __init__(self, settings, client, store, resolver=public_dns):
        self.settings, self.client, self.store, self.resolver = settings, client, store, resolver

    async def _read(self, url, headers=None, *, method="GET", payload=None):
        async with self.client.stream(method, url, headers=headers, json=payload,
                                      timeout=10, follow_redirects=False) as response:
            if response.is_redirect:
                return response.status_code, response.headers, b""
            if response.status_code != 200:
                raise RetrievalError("http_" + str(response.status_code))
            if not any(kind in response.headers.get("content-type", "") for kind in ("text/html", "application/json")):
                raise RetrievalError("unsupported_content_type")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1_500_000:
                    raise RetrievalError("body_too_large")
            return response.status_code, response.headers, bytes(body)

    async def search(self, topic):
        if self.settings.search_provider == "off":
            return []
        key = self.settings.search_api_key.get_secret_value().strip()
        if not key:
            raise RetrievalError("search_key_missing")
        query = TOPICS[topic][0]
        status, _, body = await self._read("https://api.tavily.com/search",
            {"Authorization": "Bearer " + key, "Accept": "application/json"}, method="POST",
            payload={"query": query, "topic": "general", "search_depth": "basic", "max_results": 6,
                     "include_domains": sorted(ALLOWED_HOSTS), "include_answer": False,
                     "include_raw_content": False, "include_images": False, "auto_parameters": False})
        if status != 200:
            raise RetrievalError("http_" + str(status))
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise RetrievalError("invalid_search_response") from None
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise RetrievalError("invalid_search_response")
        links = []
        for item in data["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            try:
                url = safe_url(item["url"])
            except (ValueError, RetrievalError):
                continue
            if url not in links:
                links.append(url)
        return links[:5]

    async def fetch(self, url):
        for _ in range(3):
            url = safe_url(url)
            await self.resolver(urlsplit(url).hostname)
            status, headers, body = await self._read(url)
            if status in (301, 302, 303, 307, 308):
                url = urljoin(url, headers.get("location", ""))
                continue
            return self.store.put(extract_document(url, body))
        raise RetrievalError("redirect_limit")

    async def retrieve(self, topics, force=False):
        topics = list(dict.fromkeys(topic for topic in topics if topic in TOPICS))[:2] or ["updates"]
        bundle = {"documents": [], "events": [], "warnings": [], "queries": [], "cache_hit": False}
        if self.settings.search_provider == "off":
            bundle["warnings"].append("联网检索已关闭；没有使用模型记忆补充游戏事实。")
            return bundle
        key = "web-v2:" + self.settings.search_provider + ":" + ",".join(sorted(topics))
        cached = self.store.cached(key) if not force else []
        if cached:
            return {**bundle, "documents": cached, "cache_hit": True, "events": [{"type": "cache", "count": len(cached)}]}
        try:
            async with asyncio.timeout(self.settings.evidence_timeout_seconds):
                searches = await asyncio.gather(*(self.search(topic) for topic in topics), return_exceptions=True)
                urls = []
                for topic, found in zip(topics, searches):
                    bundle["queries"].append(TOPICS[topic][0])
                    if isinstance(found, list):
                        found = [url for url in found if specific_url(url)]
                    if isinstance(found, BaseException) or not found:
                        bundle["events"].append({"type": "search_unavailable", "topic": topic})
                        if isinstance(found, RetrievalError) and str(found) == "search_key_missing":
                            bundle["warnings"].append("Tavily 搜索密钥未配置，请在本地 .env 设置 COACH_SEARCH_API_KEY。")
                        bundle["warnings"].append("搜索未返回可用可信来源，尝试直接读取已登记的相关资料；不代表已穷尽最新资料。")
                        found = [TOPICS[topic][1]]
                    urls.extend([TOPICS[topic][1], *found[:1]])
                for url in list(dict.fromkeys(urls))[:4]:
                    try:
                        document = await self.fetch(url)
                        if not relevant(document, topics):
                            bundle["events"].append({"type": "irrelevant_body", "document_id": document["id"]})
                            continue
                        bundle["documents"].append(document)
                        bundle["events"].append({"type": "fetched", "document_id": document["id"]})
                    except (httpx.HTTPError, RetrievalError, ValueError, OSError):
                        bundle["events"].append({"type": "fetch_failed", "url": url})
        except TimeoutError:
            bundle["warnings"].append("取证时间预算已用完，仅保留已读取的正文。")
        if any(event["type"] in {"fetch_failed", "irrelevant_body"} for event in bundle["events"]):
            bundle["warnings"].append("部分候选网页无法读取或与主题不匹配，仅使用通过正文检查的资料。")
        if not bundle["documents"]:
            bundle["warnings"].append("没有取得可引用的网页正文，无法验证当前游戏机制。")
        bundle["warnings"] = list(dict.fromkeys(bundle["warnings"]))
        if bundle["documents"] and not bundle["warnings"]:
            self.store.cache(key, bundle["documents"], self.settings.evidence_ttl_hours)
        logger.info("research_retrieved documents=%s cache=%s events=%s", len(bundle["documents"]),
                    bundle["cache_hit"], len(bundle["events"]))
        return bundle
