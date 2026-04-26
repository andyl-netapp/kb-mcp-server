"""
HTTP client for kb.netapp.com.

Handles authenticated requests for searching KB articles and
fetching article content using stored session cookies.

kb.netapp.com is built on MindTouch (not Salesforce). Key facts:
  - Article URLs: /on-prem/ontap/{Category}/{Category}-KBs/<slug>
  - Category pages list articles as <a> links — we browse these for search
  - Article body is in: div.mt-content-container or script tag (JS-rendered),
    but raw HTML contains a "Sign in to view" gate unless fully authenticated
"""

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from auth_manager import get_stored_cookies, get_username

KB_BASE_URL = "https://kb.netapp.com"

# Category listing pages — browsed to implement keyword search
# kb.netapp.com search is JS-rendered; these category pages return article links in HTML
#
# Verified paths as of 2026-04. Each page returns article <a> links in raw HTML.
# Categories without a -KBs sub-page (da/Host-Utilities, DM/VAAI, DM/REST-API) are omitted.
KB_CATEGORY_BROWSE_PATHS = [
    # Performance
    "/on-prem/ontap/Perf/Perf-KBs",
    # Operating System
    "/on-prem/ontap/Ontap_OS/OS-KBs",
    # Upgrade
    "/on-prem/ontap/Upgrade/Upgrade-KBs",
    # MetroCluster
    "/on-prem/ontap/mc/MC-KBs",
    # Mediator
    "/on-prem/ontap/Mediator/Mediator-KBs",
    # Hardware
    "/on-prem/ontap/OHW/OHW-KBs",
    # Data Access
    "/on-prem/ontap/da/NAS",
    "/on-prem/ontap/da/SAN",
    "/on-prem/ontap/da/XCP",
    # Data Protection
    "/on-prem/ontap/DP/SnapMirror",
    "/on-prem/ontap/DP/SnapLock",
    "/on-prem/ontap/DP/SnapRestore",
    "/on-prem/ontap/DP/NDMP",
    # Data Management
    "/on-prem/ontap/DM/FlexGroup",
    "/on-prem/ontap/DM/Encryption",
    "/on-prem/ontap/DM/Efficiency",
    "/on-prem/ontap/DM/FabricPool",
    "/on-prem/ontap/DM/System_Manager",
]

# Link href path segments that identify article links vs navigation links
ARTICLE_PATH_MARKERS = [
    "/Perf-KBs/",
    "/OS-KBs/",
    "/Upgrade-KBs/",
    "/MC-KBs/",
    "/Mediator-KBs/",
    "/OHW-KBs/",
    "/NAS-KBs/",
    "/SAN-KBs/",
    "/XCP-KBs/",
    "/SnapMirror-KBs/",
    "/SnapLock-KBs/",
    "/SnapDiff-KBs/",
    "/SnapRestore-KBs/",
    "/NDMP-KBs/",
    "/FlexGroup-KBs/",
    "/Encryption-KBs/",
    "/Efficiency-KBs/",
    "/FabricPool-KBs/",
    "/SM-KBs/",
    "/article/",
]

REQUEST_TIMEOUT = 30

_COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://kb.netapp.com/",
}


# ---------------------------------------------------------------------------
# Session builder
# ---------------------------------------------------------------------------

def _build_session(username: str = None) -> requests.Session:
    """Create an authenticated requests.Session using stored cookies."""
    if username is None:
        username = get_username()

    cookie_data = get_stored_cookies(username)
    cookies_list = cookie_data.get("cookies", [])

    jar = requests.cookies.RequestsCookieJar()
    for c in cookies_list:
        jar.set(
            c["name"],
            c["value"],
            domain=c.get("domain", ".netapp.com"),
            path=c.get("path", "/"),
        )

    session = requests.Session()
    session.cookies = jar
    session.headers.update(_COMMON_HEADERS)
    return session


def _check_auth_response(response: requests.Response) -> Optional[dict]:
    """Return an error dict if the response indicates an auth failure, else None."""
    if response.status_code in (401, 403):
        return {
            "error": (
                "Authentication failed (HTTP {}).\n"
                "Please run Set-KBCookies.ps1 to refresh your login."
            ).format(response.status_code)
        }
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_kb(
    query: str,
    product: str = None,
    category: str = None,
    limit: int = 10,
) -> dict:
    """
    Search KB articles on kb.netapp.com by browsing category listing pages.

    kb.netapp.com search is JavaScript-rendered and cannot be queried via
    a simple HTTP GET. Instead, we browse known category pages (which do
    return article links in static HTML) and filter by keyword match.

    Args:
        query:    Free-text search query (keywords matched against article titles).
        product:  Unused (all results are ONTAP; extend paths for other products).
        category: Optional hint to narrow which category pages to browse
                  (e.g. 'Perf', 'os', 'Data_Access').
        limit:    Maximum number of results (default 10).

    Returns:
        dict with 'results' list or 'error' key.
    """
    session = _build_session()
    keywords = [kw.lower() for kw in re.split(r"\s+", query) if len(kw) > 2]
    results = []
    seen_urls = set()

    paths_to_check = list(KB_CATEGORY_BROWSE_PATHS)
    if category:
        cat_clean = category.strip("/")
        guessed = f"/on-prem/ontap/{cat_clean}/{cat_clean}-KBs"
        paths_to_check.insert(0, guessed)

    for path in paths_to_check:
        if len(results) >= limit * 3:  # gather extra then re-sort
            break
        try:
            resp = session.get(f"{KB_BASE_URL}{path}", timeout=REQUEST_TIMEOUT)
            auth_err = _check_auth_response(resp)
            if auth_err:
                return auth_err
            if resp.status_code != 200:
                continue

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                title = a.get_text(strip=True)
                if not title or len(title) < 8:
                    continue
                if not any(m in href for m in ARTICLE_PATH_MARKERS):
                    continue
                if not href.startswith("http"):
                    href = KB_BASE_URL + href
                if href in seen_urls:
                    continue

                title_lower = title.lower()
                score = sum(1 for kw in keywords if kw in title_lower)
                if score > 0:
                    seen_urls.add(href)
                    results.append({"title": title, "url": href, "snippet": "", "score": score})

        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    for r in results:
        r.pop("score", None)
    results = results[:limit]

    return {
        "query": query,
        "result_count": len(results),
        "results": results,
        "note": (
            "Results matched from KB category pages. "
            "Use kb_get_article to read full content."
        ),
    }


def get_article(article_id_or_url: str) -> dict:
    """
    Fetch a KB article by ID, slug path, or full URL.

    Accepts:
      - Article slug URL: e.g. 'on-prem/ontap/Perf/Perf-KBs/How_to_...'
      - Full URL:         e.g. 'https://kb.netapp.com/on-prem/...'

    Returns:
        dict with 'title', 'url', 'content', 'metadata' or 'error' key.
    """
    session = _build_session()
    url = _resolve_article_url(article_id_or_url)

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        auth_err = _check_auth_response(resp)
        if auth_err:
            return auth_err
        resp.raise_for_status()

        if "json" in resp.headers.get("content-type", ""):
            return resp.json()

        return _parse_article_html(resp.text, url)

    except requests.RequestException as e:
        return {"error": str(e)}


def fetch_kb_url(url: str) -> dict:
    """
    Fetch any kb.netapp.com or netapp.com URL and return parsed content.

    Returns:
        dict with 'title', 'url', 'content' and optionally 'links' or 'error'.
    """
    parsed = urlparse(url)
    if not parsed.netloc.endswith("netapp.com"):
        return {"error": f"Only netapp.com URLs are allowed. Got: {url}"}
    if not parsed.scheme:
        url = "https://" + url

    session = _build_session()

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        auth_err = _check_auth_response(resp)
        if auth_err:
            return auth_err
        resp.raise_for_status()

        if "json" in resp.headers.get("content-type", ""):
            return resp.json()

        return _parse_article_html(resp.text, url)

    except requests.RequestException as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# HTML parsers
# ---------------------------------------------------------------------------

def _resolve_article_url(id_or_url: str) -> str:
    """Convert an article ID / partial path / full URL into an absolute URL."""
    id_or_url = id_or_url.strip()
    if id_or_url.startswith("http"):
        return id_or_url
    # Bare numeric ID
    if re.match(r"^\d+$", id_or_url):
        return f"{KB_BASE_URL}/on-prem/ontap/article/{id_or_url}"
    # Partial path
    return urljoin(KB_BASE_URL + "/", id_or_url.lstrip("/"))


def _parse_article_html(html: str, url: str) -> dict:
    """
    Parse a kb.netapp.com page and extract:
    - title, metadata
    - main article body text (from mt-content-container / elm-content-container)
    - article links listed on the page (for category/listing pages)
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # --- Title ---
        title = ""
        for sel in ["h1", "title"]:
            tag = soup.select_one(sel)
            if tag:
                title = tag.get_text(strip=True)
                if title and title != "NetApp":
                    break

        # --- Metadata ---
        metadata = {}
        for meta in soup.select("meta[name]"):
            name = meta.get("name", "")
            content = meta.get("content", "")
            if name and content and name in ("description", "keywords", "product", "version"):
                metadata[name] = content

        # --- Article body: MindTouch uses mt-content-container ---
        # We try progressively broader containers so we don't miss content
        content_text = ""
        for sel in [
            "div.mt-content-container",
            "article.elm-content-container",
            "div.elm-content-container",
            "main",
            "article",
        ]:
            tag = soup.select_one(sel)
            if tag:
                # Remove chrome/noise elements inside
                for noise in tag.select("nav, .mt-social-share, .mt-page-stats, "
                                        ".mt-translate-container, script, style, "
                                        ".elm-related-articles-container"):
                    noise.decompose()
                text = tag.get_text(separator="\n", strip=True)
                if len(text) > len(content_text):
                    content_text = text

        # --- Article links (useful for category pages) ---
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            link_title = a.get_text(strip=True)
            if link_title and len(link_title) > 8 and any(m in href for m in ARTICLE_PATH_MARKERS):
                if not href.startswith("http"):
                    href = KB_BASE_URL + href
                links.append({"title": link_title, "url": href})

        # Clean up content
        content_text = re.sub(r"\n{3,}", "\n\n", content_text).strip()

        result: dict = {
            "url": url,
            "title": title,
            "metadata": metadata,
            "content": content_text[:15000],
            "truncated": len(content_text) > 15000,
        }
        if links:
            result["article_links"] = links

        return result

    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return {
            "url": url,
            "title": "KB Page",
            "note": "Install beautifulsoup4 for cleaner parsing.",
            "content": text[:15000],
            "truncated": len(text) > 15000,
        }
