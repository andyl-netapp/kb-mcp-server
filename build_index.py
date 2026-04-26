#!/usr/bin/env python3
"""
KB NetApp Bulk Article Indexer
==============================
Uses Playwright browser + stored SSO cookies to fetch and index
all articles from the 18 KB categories.

The key advantage over plain HTTP fetching: Playwright executes JavaScript,
so articles that normally show "Sign in to view the entire content" are
rendered in full after the SSO cookies are injected.

Prerequisites:
  - Run Set-KBCookies.ps1 first to capture session cookies
  - pip install -r requirements_v2.txt

Usage:
  python build_index.py                           # index all categories
  python build_index.py --teams Performance,NAS   # specific teams only
  python build_index.py --delay 1.5               # seconds between requests (default: 1.5)
  python build_index.py --limit 50                # max articles, for testing
  python build_index.py --force                   # re-index already-indexed articles
  python build_index.py --headless                # browser runs invisibly (faster)
  python build_index.py --save-every 5            # save index every N articles (default: 10)
"""

import argparse
import os
import sys
import time
from typing import List, Optional

# ---------------------------------------------------------------------------
# Ensure kb_client_v2 can be imported (same directory)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth_manager
import kb_client_v2 as kbc


# ---------------------------------------------------------------------------
# Article content extractor using a Playwright page
# ---------------------------------------------------------------------------

_LOGIN_WALL_PHRASES = [
    "sign in to view the entire content",
    "sign in\nnew to netapp",
]

_CONTENT_SELECTORS = [
    ".mt-content-container",
    ".article-body",
    "#mt-content",
    "#content .body",
    "article",
    ".page-content",
    "main",
]


def _page_is_gated(text: str) -> bool:
    """
    Return True if the rendered page shows an authentication wall.
    
    Only checks the first 300 chars — a real auth wall shows at the very top.
    Long content (>300 chars) with a mention of 'sign in' later is NOT a gated page.
    """
    if len(text) > 300:
        return False  # substantial content means article loaded successfully
    lower = text.lower()
    return any(phrase in lower for phrase in _LOGIN_WALL_PHRASES)


def _extract_text_from_page(page) -> Optional[str]:
    """
    Pull the rendered HTML from a Playwright page and extract article text
    using the same BeautifulSoup parser used by kb_client_v2.
    """
    html = page.content()
    result = kbc._parse_article_html(html, page.url)
    content = result.get("content", "").strip()
    if not content or _page_is_gated(content):
        return None
    return content


def _get_article_title_from_page(page) -> str:
    """Extract title from the rendered page."""
    html = page.content()
    result = kbc._parse_article_html(html, page.url)
    return result.get("title", "").strip()


# ---------------------------------------------------------------------------
# Core indexer
# ---------------------------------------------------------------------------

def build_index(
    teams: Optional[List[str]] = None,
    delay: float = 1.5,
    limit: Optional[int] = None,
    force: bool = False,
    headless: bool = False,
    save_every: int = 10,
) -> None:
    # --- Auth check ---
    username = auth_manager.get_username()
    if not auth_manager.has_stored_cookies(username):
        print("[ERROR] No session cookies found. Run Set-KBCookies.ps1 first.", file=sys.stderr)
        sys.exit(1)
    if auth_manager.is_cookies_expired(username):
        print("[ERROR] Session cookies have expired. Re-run Set-KBCookies.ps1.", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Authenticated as '{username}'")

    # --- Collect article URLs via HTTP (category pages are server-side rendered) ---
    print("\n[...] Scanning category pages for article URLs...")
    session = kbc._build_session()
    all_candidates = kbc._browse_categories(session)

    # Filter by team if requested
    if teams:
        teams_lower = [t.strip().lower() for t in teams]
        all_candidates = [c for c in all_candidates if c["team"].lower() in teams_lower]

    print(f"[OK] Found {len(all_candidates)} articles across {len(set(c['team'] for c in all_candidates))} teams")

    # Filter already-indexed unless --force
    index = kbc.get_index()
    if not force:
        before = len(all_candidates)
        all_candidates = [c for c in all_candidates if not index.has_article(c["url"])]
        skipped = before - len(all_candidates)
        if skipped:
            print(f"[OK] Skipping {skipped} already-indexed articles (use --force to re-index)")

    if limit:
        all_candidates = all_candidates[:limit]

    total = len(all_candidates)
    if total == 0:
        print("\n[OK] Nothing to index. All articles are already up to date.")
        return

    print(f"\n[...] Indexing {total} articles using browser rendering...\n")

    # --- Launch Playwright using the same persistent profile as login_helper.py ---
    # This gives us the full auth state (cookies + localStorage tokens) captured during SSO login.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] Playwright not installed. Run: pip install playwright", file=sys.stderr)
        sys.exit(1)

    import os
    edge_session_dir = os.path.join(os.path.expanduser("~"), ".copilot", ".netapp_browser_data")
    if not os.path.exists(edge_session_dir):
        print(
            "[ERROR] Browser profile not found. Run Set-KBCookies.ps1 first to log in.",
            file=sys.stderr,
        )
        sys.exit(1)

    _bad_flags = [
        "--disable-sync",
        "--disable-extensions",
        "--disable-background-networking",
        "--no-sandbox",
    ]

    indexed = 0
    failed  = 0
    gated   = 0

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=edge_session_dir,
            channel="msedge",
            headless=headless,
            slow_mo=0,
            viewport={"width": 1280, "height": 800},
            ignore_default_args=_bad_flags,
        )
        page = context.new_page()

        for i, candidate in enumerate(all_candidates, 1):
            url   = candidate["url"]
            team  = candidate["team"]
            domain = candidate["domain"]
            title_hint = candidate["title"]

            print(f"[{i:4d}/{total}] {team:15s} | {title_hint[:60]}")

            # Recreate page if it was closed (e.g. browser auto-closed a tab)
            try:
                if page.is_closed():
                    page = context.new_page()
            except Exception:
                page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # Wait for the article body to fully load (KB lazy-loads content via JS).
                # First wait for the container to appear, then wait until it has real content.
                for sel in _CONTENT_SELECTORS:
                    try:
                        page.wait_for_selector(sel, timeout=15_000)
                        # Wait until the container text is substantial (not just auth wall)
                        js = f"""
                            () => {{
                                const el = document.querySelector('{sel}');
                                return el && el.innerText && el.innerText.length > 500 &&
                                       !el.innerText.toLowerCase().includes('sign in to view');
                            }}
                        """
                        page.wait_for_function(js, timeout=20_000)
                        break
                    except Exception:
                        continue
            except Exception as e:
                print(f"           ↳ SKIP (navigation error: {type(e).__name__})")
                failed += 1
                time.sleep(delay)
                continue

            try:
                content = _extract_text_from_page(page)
            except Exception as e:
                print(f"           ↳ SKIP (page closed during extract: {type(e).__name__})")
                failed += 1
                page = context.new_page()
                time.sleep(delay)
                continue

            if content is None:
                gated += 1
                time.sleep(delay)
                continue

            try:
                title = _get_article_title_from_page(page) or title_hint
            except Exception:
                title = title_hint

            try:
                kbc._index_article(
                    content=content,
                    url=url,
                    title=title,
                    domain=domain,
                    team=team,
                )
                indexed += 1
                print(f"           ↳ OK ({len(content):,} chars)")
            except Exception as e:
                print(f"           ↳ SKIP (index error: {e})")
                failed += 1

            # Periodic save
            if indexed % save_every == 0:
                index.save()
                print(f"           [Saved index: {index.total_chunks} chunks / {index.total_articles} articles]")

            time.sleep(delay)

        context.close()

    # Final save
    index.save()

    # Summary
    print("\n" + "=" * 60)
    print(f"  Done!")
    print(f"  Indexed : {indexed}")
    print(f"  Gated   : {gated}  (still requires browser auth — cookies may need refresh)")
    print(f"  Failed  : {failed}")
    print(f"  Index   : {index.total_chunks} chunks from {index.total_articles} articles")
    print(f"  Saved to: {kbc.INDEX_FILE}")
    print("=" * 60)

    if gated > 0:
        print(f"\n[TIP] {gated} articles were still gated. Try re-running Set-KBCookies.ps1 and retrying.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(
        description="Bulk-index all KB articles using Playwright browser rendering.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--teams",
        help="Comma-separated team names to index (e.g. Performance,FlexGroup,NAS). Default: all.",
        default=None,
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Seconds to wait between article requests (default: 1.5).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of articles to index (useful for testing).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-index articles that are already in the index.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (no visible window, faster).",
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=10,
        dest="save_every",
        help="Save index to disk every N articles (default: 10).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    teams = [t.strip() for t in args.teams.split(",")] if args.teams else None
    build_index(
        teams=teams,
        delay=args.delay,
        limit=args.limit,
        force=args.force,
        headless=args.headless,
        save_every=args.save_every,
    )
