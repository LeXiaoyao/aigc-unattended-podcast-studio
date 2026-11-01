from __future__ import annotations

import email.utils
import html
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

try:
    from .topic_keywords import normalize_space, strip_html
except ImportError:  # pragma: no cover
    from topic_keywords import normalize_space, strip_html


COMMON_FEED_PATHS = ["/rss", "/feed", "/feed.xml", "/rss.xml", "/episodes.rss"]
USER_AGENT = "Mozilla/5.0 (compatible; Xiaoyuzhou-AI-Podcast-Competitor-Radar/1.0)"
NAV_TITLES = {
    "episodes",
    "hosts",
    "search",
    "subscribe",
    "rss",
    "apple podcasts",
    "amazon music",
    "spotify",
    "overcast",
}


@dataclass
class FetchResult:
    url: str
    body: str
    content_type: str


class FeedLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.feed_links: list[str] = []
        self.meta_description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "link":
            rel = attr.get("rel", "").lower()
            typ = attr.get("type", "").lower()
            href = attr.get("href", "")
            if "alternate" in rel and ("rss" in typ or "atom" in typ or "xml" in typ) and href:
                self.feed_links.append(href)
        if tag.lower() == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            if name in {"description", "og:description"} and attr.get("content"):
                self.meta_description = attr["content"]


class EpisodeHTMLParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[dict[str, str]] = []
        self._current_href = ""
        self._current_text: list[str] = []
        self._in_title = False
        self.title = ""
        self.times: list[str] = []
        self.meta_description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a" and attr.get("href"):
            self._current_href = urllib.parse.urljoin(self.base_url, attr["href"])
            self._current_text = []
        elif tag.lower() == "title":
            self._in_title = True
        elif tag.lower() == "time":
            value = attr.get("datetime") or attr.get("title") or ""
            if value:
                self.times.append(value)
        elif tag.lower() == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            if name in {"description", "og:description"} and attr.get("content"):
                self.meta_description = attr["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            text = normalize_space("".join(self._current_text))
            if text:
                self.links.append({"title": html.unescape(text), "url": self._current_href})
            self._current_href = ""
            self._current_text = []
        elif tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)
        if self._in_title:
            self.title += data


def fetch_url(url: str, timeout: int = 12) -> FetchResult:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        content_type = response.headers.get("content-type", "")
        charset = response.headers.get_content_charset() or "utf-8"
    return FetchResult(url=url, body=raw.decode(charset, errors="replace"), content_type=content_type)


def discover_feed_urls(homepage: str, html_body: str | None = None) -> list[str]:
    urls: list[str] = []
    if html_body:
        parser = FeedLinkParser()
        parser.feed(html_body)
        for href in parser.feed_links:
            urls.append(urllib.parse.urljoin(homepage, href))
    parsed = urllib.parse.urlparse(homepage)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.netloc.endswith(".fireside.fm"):
        show_id = parsed.netloc.split(".")[0]
        urls.append(f"https://feeds.fireside.fm/{show_id}/rss")
    for path in COMMON_FEED_PATHS:
        urls.append(urllib.parse.urljoin(base, path))
        urls.append(urllib.parse.urljoin(homepage.rstrip("/") + "/", path.lstrip("/")))
    deduped: list[str] = []
    for url in urls:
        if url not in deduped:
            deduped.append(url)
    return deduped


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _xml_text(node: ET.Element, names: list[str]) -> str:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return strip_html(found.text)
        found = node.find(f"{{*}}{name}")
        if found is not None and found.text:
            return strip_html(found.text)
    return ""


def _xml_link(node: ET.Element) -> str:
    link = _xml_text(node, ["link"])
    if link:
        return link
    for found in node.findall("{*}link"):
        href = found.attrib.get("href")
        if href:
            return href
    return ""


def parse_feed_items(
    xml_body: str,
    account: dict[str, Any],
    source_role: str,
    source_url: str,
    lookback_hours: int,
    competitor_lookback_days: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    try:
        root = ET.fromstring(xml_body.strip())
    except ET.ParseError:
        return []
    entries = root.findall(".//item") or root.findall(".//{*}entry")
    items: list[dict[str, Any]] = []
    for entry in entries[:30]:
        title = _xml_text(entry, ["title"])
        url = _xml_link(entry)
        published = _xml_text(entry, ["pubDate", "published", "updated", "date"])
        summary = _xml_text(entry, ["description", "summary", "subtitle", "encoded"])
        if not title and not summary:
            continue
        parsed_time = parse_datetime(published)
        if parsed_time:
            age = now - parsed_time.astimezone(timezone.utc)
            if age > timedelta(days=competitor_lookback_days):
                continue
            if source_role == "competitor" and age > timedelta(hours=lookback_hours):
                if len(items) >= 3:
                    continue
        elif len(items) >= 8:
            continue
        items.append(
            normalize_item(
                account=account,
                source_role=source_role,
                title=title,
                url=url or source_url,
                published_at=published,
                summary=summary,
                shownotes=summary,
                raw_text=summary,
                source_status="rss",
            )
        )
    return items


def parse_html_episode_items(html_body: str, account: dict[str, Any], source_role: str, homepage: str) -> list[dict[str, Any]]:
    parser = EpisodeHTMLParser(homepage)
    parser.feed(html_body)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for link in parser.links:
        raw_title = normalize_space(link.get("title", ""))
        title = clean_episode_title(raw_title)
        url = link.get("url", "")
        lowered = f"{title} {url}".lower()
        if not title or url in seen:
            continue
        if title.strip().lower() in NAV_TITLES:
            continue
        if any(skip in lowered for skip in ("privacy", "terms", "contact", "login", "archive", "about", "/hosts/")):
            continue
        if source_role == "competitor" and not any(mark in lowered for mark in ("episode", "podcast", "节目", "blog", "post", "claw", "espresso", "xiaoyuzhoufm.com/episode")):
            if len(title) < 12:
                continue
        seen.add(url)
        items.append(
            normalize_item(
                account=account,
                source_role=source_role,
                title=title,
                url=url,
                published_at=parser.times[0] if parser.times else "",
                summary=parser.meta_description,
                shownotes=parser.meta_description,
                raw_text=f"{raw_title} {parser.meta_description}",
                source_status="html",
            )
        )
        if len(items) >= 10:
            break
    if not items and parser.title:
        items.append(
            normalize_item(
                account=account,
                source_role=source_role,
                title=normalize_space(parser.title),
                url=homepage,
                published_at=parser.times[0] if parser.times else "",
                summary=parser.meta_description,
                shownotes=parser.meta_description,
                raw_text=strip_html(html_body[:5000]),
                source_status="html",
            )
        )
    return items


def clean_episode_title(text: str) -> str:
    text = normalize_space(re.sub(r"\d+\.css-[A-Za-z0-9_-]+.*$", "", text or ""))
    for marker in ("☀️", "🧠", "🙇", "【本期简介】", "今天是", "欢迎加入", "欢迎添加", "💡", "👉"):
        if marker in text:
            text = text.split(marker, 1)[0]
    text = normalize_space(text)
    if len(text) > 180:
        text = text[:180].rstrip() + "..."
    return text


def normalize_item(
    account: dict[str, Any],
    source_role: str,
    title: str,
    url: str,
    published_at: str,
    summary: str,
    shownotes: str,
    raw_text: str,
    source_status: str,
    fetch_error: str = "",
) -> dict[str, Any]:
    return {
        "source_role": source_role,
        "account_id": str(account.get("id") or ""),
        "account_name": str(account.get("name") or ""),
        "title": normalize_space(title),
        "url": url,
        "published_at": normalize_space(published_at),
        "summary": strip_html(summary),
        "shownotes": strip_html(shownotes),
        "raw_text": strip_html(raw_text),
        "topics": [],
        "entities": [],
        "keywords": [],
        "source_status": source_status,
        "fetch_error": fetch_error,
    }


def failed_item(account: dict[str, Any], source_role: str, error: str) -> dict[str, Any]:
    return normalize_item(
        account=account,
        source_role=source_role,
        title="",
        url=str(account.get("homepage") or ""),
        published_at="",
        summary="",
        shownotes="",
        raw_text="",
        source_status="failed",
        fetch_error=error,
    )


def fetch_account_items(
    account: dict[str, Any],
    source_role: str,
    lookback_hours: int = 48,
    competitor_lookback_days: int = 7,
    timeout: int = 12,
) -> list[dict[str, Any]]:
    homepage = str(account.get("homepage") or "")
    if not homepage:
        return [failed_item(account, source_role, "missing homepage")]
    errors: list[str] = []
    html_body = ""
    try:
        homepage_result = fetch_url(homepage, timeout=timeout)
        html_body = homepage_result.body
        if re.search(r"<rss|<feed", html_body[:500], re.I):
            items = parse_feed_items(html_body, account, source_role, homepage, lookback_hours, competitor_lookback_days)
            if items:
                return items
    except Exception as exc:
        errors.append(f"homepage: {exc}")

    for feed_url in discover_feed_urls(homepage, html_body):
        try:
            result = fetch_url(feed_url, timeout=timeout)
            items = parse_feed_items(result.body, account, source_role, feed_url, lookback_hours, competitor_lookback_days)
            if items:
                return items
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{feed_url}: {exc}")

    if html_body:
        items = parse_html_episode_items(html_body, account, source_role, homepage)
        if items:
            return items
    return [failed_item(account, source_role, "; ".join(errors[-4:]) or "no feed or episode items discovered")]
