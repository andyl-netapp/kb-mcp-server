#!/usr/bin/env python3
"""
Bulk index Performance KB articles using HTTP (fast, no Playwright required).

Uses the same HTTP session and indexing pipeline as the MCP server itself.
Each article takes ~2-5s vs 30-45s with Playwright.

Usage:
    python build_index_http.py [--force] [--limit N] [--delay SECS]
"""
import argparse
import os
import sys
import time

# Set CA bundle before any SSL-using imports
_ca = os.path.expanduser("~/.copilot/ca-bundle.pem")
if os.path.exists(_ca):
    os.environ.setdefault("KB_CA_BUNDLE", _ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
    os.environ.setdefault("SSL_CERT_FILE", _ca)

sys.path.insert(0, os.path.dirname(__file__))

from kb_client import (
    _browse_categories,
    _build_session,
    _index_article,
    _parse_article_html,
    EmbeddingEngine,
    get_index,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk index Performance KB via HTTP")
    parser.add_argument("--force",  action="store_true",
                        help="Re-index articles already in the index")
    parser.add_argument("--limit",  type=int, default=0,
                        help="Stop after N articles (0 = all)")
    parser.add_argument("--delay",  type=float, default=0.3,
                        help="Pause between requests in seconds (default: 0.3)")
    parser.add_argument("--teams",  default="Performance",
                        help="Comma-separated team names to index (default: Performance)")
    args = parser.parse_args()

    teams = [t.strip() for t in args.teams.split(",")]

    # --- Embedding engine warm-up ---
    engine = EmbeddingEngine.get()
    print(f"[*] Embedding engine available: {engine.is_available()}")

    # --- HTTP session ---
    print("[*] Building HTTP session...")
    session = _build_session()

    # --- Discover articles ---
    all_candidates = []
    for team in teams:
        print(f"[*] Scanning category pages for team: {team}...")
        cands = _browse_categories(session, team_filter=team)
        print(f"    Found {len(cands)} articles")
        all_candidates.extend(cands)

    total = len(all_candidates)
    if total == 0:
        print("[!] No articles found. Check VPN/auth.")
        sys.exit(1)

    index = get_index()

    indexed = 0
    skipped = 0
    failed  = 0
    empty   = 0

    for i, cand in enumerate(all_candidates, 1):
        url    = cand["url"]
        title  = cand.get("title", "")
        domain = cand.get("domain", "ONTAP")
        team   = cand.get("team", "Performance")

        # Respect --limit (counts successfully indexed articles)
        if args.limit and indexed >= args.limit:
            break

        if not args.force and index.has_article(url):
            skipped += 1
            continue

        print(f"[{i:4d}/{total}] {title[:70]}")

        try:
            resp = session.get(url, timeout=30)
            if resp.status_code != 200:
                print(f"       ↳ SKIP HTTP {resp.status_code}")
                failed += 1
                continue

            result = _parse_article_html(resp.text, url)
            content   = result.get("content", "")
            art_title = result.get("title", "") or title

            if not content or len(content) < 200:
                print(f"       ↳ SKIP empty ({len(content)} chars)")
                empty += 1
                continue

            _index_article(
                content=content,
                url=url,
                title=art_title,
                domain=domain,
                team=team,
                published_date=result.get("metadata", {}).get("date"),
            )
            indexed += 1
            vec_flag = "+" if engine.is_available() else "-"
            print(f"       ↳ OK [{vec_flag}vec] ({len(content):,} chars)")

        except Exception as exc:
            print(f"       ↳ ERROR: {exc}")
            failed += 1

        if args.delay:
            time.sleep(args.delay)

    print(f"\n{'='*60}")
    print(f"Done. Indexed: {indexed}  Skipped: {skipped}  "
          f"Empty: {empty}  Failed: {failed}")
    print(f"Total in index: {len(index.get_filtered_chunks())}")


if __name__ == "__main__":
    main()
