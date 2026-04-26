#!/usr/bin/env python3
"""
KB NetApp MCP Server v2 — Production-Grade
==========================================
New features over kb_mcp.py (v1):
  - kb_semantic_search : hybrid BM25 + sentence-transformer vector search
  - kb_keyword_lookup  : exact-match search for error codes / internal terms
  - Metadata filtering : domain, team, valid_after for both search tools
  - Auto-indexing      : kb_get_article chunks and embeds fetched articles
  - Strict guardrail   : all search responses carry a no-hallucination prompt

Authentication is shared with v1 (same auth_manager / cookie store).

PORTABLE: All configuration is via environment variables:
  KB_USERNAME : Your NetApp email/username (default: current system user)

Setup:
  1. pip install -r requirements_v2.txt
  2. .\\Set-KBCookies.ps1         ← shared with v1, only needed once
  3. Add to your mcp-config.json:
       "kb-netapp-v2": {
         "command": "python",
         "args": ["C:\\\\...\\\\kb_mcp_v2.py"],
         "env": { "KB_USERNAME": "your_username" }
       }
  4. /restart in Copilot
"""

import asyncio
import json
import logging
import threading
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import auth_manager
import kb_client_v2 as kbc
import login_helper

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("kb-mcp-v2")


# ---------------------------------------------------------------------------
# Eager warm-up: pre-load models in background thread so the first search
# call doesn't timeout waiting for model downloads / initialisation.
# ---------------------------------------------------------------------------

_warmup_done = threading.Event()


def _warmup() -> None:
    """Pre-load article index, embedding model, and reranker model."""
    try:
        logger.info("Warm-up: loading article index...")
        kbc.get_index()

        logger.info("Warm-up: loading embedding model...")
        engine = kbc.EmbeddingEngine.get()
        if engine.is_available():
            engine.encode(["warmup"])  # triggers actual model load

        logger.info("Warm-up: loading reranker model...")
        reranker = kbc.RerankEngine.get()
        if reranker.is_available():
            reranker.rerank("warmup", [{"title": "warmup", "snippet": "warmup"}])

        logger.info("Warm-up complete.")
    except Exception as exc:
        logger.warning("Warm-up failed (non-fatal): %s", exc)
    finally:
        _warmup_done.set()


# Fire and forget — runs in parallel with MCP server startup
threading.Thread(target=_warmup, daemon=True, name="kb-warmup").start()

server = Server("kb-netapp-mcp-v2")


# ---------------------------------------------------------------------------
# Helpers (same pattern as v1)
# ---------------------------------------------------------------------------

def _json_result(data: dict | list) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False))]


def _ensure_auth() -> Optional[str]:
    username = auth_manager.get_username()
    if not auth_manager.has_stored_cookies(username):
        return (
            "[AUTH REQUIRED] Not logged in to kb.netapp.com.\n"
            "Run Set-KBCookies.ps1 (or `python login_helper.py`) to log in first."
        )
    if auth_manager.is_cookies_expired(username):
        return (
            "⚠️  KB session cookies have expired.\n"
            "Run Set-KBCookies.ps1 (or `python login_helper.py`) to re-login."
        )
    return None


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    """Declare all tools exposed by this MCP server."""
    return [
        # ---- Auth tools (identical to v1) --------------------------------
        Tool(
            name="kb_check_auth",
            description=(
                "Check the current authentication status for kb.netapp.com.\n"
                "Returns whether the stored cookies are valid or expired, "
                "when they were captured, and when they expire."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="kb_refresh_login",
            description=(
                "Trigger a new browser-based SSO login to kb.netapp.com "
                "and refresh the stored session cookies.\n\n"
                "This opens a Chromium browser window. The user must complete "
                "the NetApp SSO login manually. The window closes automatically "
                "once login is detected.\n\n"
                "Use this tool when kb_check_auth reports expired cookies, or "
                "when search / article tools return authentication errors."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),

        # ---- NEW: Semantic search ----------------------------------------
        Tool(
            name="kb_semantic_search",
            description=(
                "Search KB articles using natural-language questions via hybrid "
                "BM25 + semantic vector similarity (sentence-transformers).\n\n"
                "Best for: conceptual questions, troubleshooting descriptions, "
                "'how do I…' or 'why does X happen' queries.\n\n"
                "Parameters:\n"
                "- query      : (required) Natural language question\n"
                "- domain     : (optional) Product domain filter, e.g. 'ONTAP'\n"
                "- team       : (optional) Area filter, e.g. 'Performance', "
                "'SnapMirror', 'FlexGroup', 'MetroCluster', 'NAS', 'SAN'\n"
                "- valid_after: (optional) ISO date 'YYYY-MM-DD' — exclude older "
                "articles (applied to indexed articles with known publish dates)\n"
                "- limit      : (optional) Max results (default: 10, max: 25)\n\n"
                "Returns ranked KB snippets with score.\n\n"
                "⚠️  STRICT GUARDRAIL: Only synthesise answers from the returned "
                "KB chunks. Do NOT fabricate information not present in results."
            ),
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language question or troubleshooting description",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Product domain filter (e.g. 'ONTAP', 'StorageGRID')",
                    },
                    "team": {
                        "type": "string",
                        "description": (
                            "Team/area filter. Valid values: Performance, OS, Upgrade, "
                            "MetroCluster, Mediator, Hardware, NAS, SAN, XCP, SnapMirror, "
                            "SnapLock, SnapRestore, NDMP, FlexGroup, Encryption, "
                            "Efficiency, FabricPool, SystemManager"
                        ),
                    },
                    "valid_after": {
                        "type": "string",
                        "description": "ISO date filter 'YYYY-MM-DD' — only articles published after this date",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default: 10)",
                        "minimum": 1,
                        "maximum": 25,
                    },
                },
            },
        ),

        # ---- NEW: Keyword lookup -----------------------------------------
        Tool(
            name="kb_keyword_lookup",
            description=(
                "Look up KB articles by an exact technical keyword: error codes, "
                "internal service/process names, command names, EMS event names, "
                "or any precise term that must appear verbatim in the article.\n\n"
                "Best for: 'ENOSPC', 'wafliron', 'kahuna', 'WAFL_CP_LIMIT', "
                "'snapmirror break', specific ONTAP command names, log message fragments.\n\n"
                "Parameters:\n"
                "- term       : (required) Exact keyword or phrase\n"
                "- domain     : (optional) Product domain filter (e.g. 'ONTAP')\n"
                "- team       : (optional) Area filter (e.g. 'Performance', 'SnapMirror')\n"
                "- valid_after: (optional) ISO date 'YYYY-MM-DD'\n"
                "- limit      : (optional) Max results (default: 10, max: 25)\n\n"
                "Returns articles and context excerpts containing the exact term.\n\n"
                "⚠️  STRICT GUARDRAIL: Only present information from the retrieved "
                "KB excerpts. Do NOT invent details about internal systems."
            ),
            inputSchema={
                "type": "object",
                "required": ["term"],
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Exact keyword, error code, or technical term to find",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Product domain filter (e.g. 'ONTAP')",
                    },
                    "team": {
                        "type": "string",
                        "description": "Team/area filter (e.g. 'Performance', 'SnapMirror')",
                    },
                    "valid_after": {
                        "type": "string",
                        "description": "ISO date filter 'YYYY-MM-DD'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10)",
                        "minimum": 1,
                        "maximum": 25,
                    },
                },
            },
        ),

        # ---- Article fetch (enhanced: auto-indexes content) ---------------
        Tool(
            name="kb_get_article",
            description=(
                "Fetch the full content of a KB article and auto-index it for "
                "future hybrid searches.\n\n"
                "After fetching, the article is semantically chunked with 15% "
                "context overlap and embedded — improving future kb_semantic_search "
                "and kb_keyword_lookup results for this article's content.\n\n"
                "Parameters:\n"
                "- article_id_or_url : (required) One of:\n"
                "    • Numeric article ID:   e.g. '1234567'\n"
                "    • Partial path:         e.g. 'on-prem/ontap/article/1234567'\n"
                "    • Full URL:             e.g. 'https://kb.netapp.com/...'\n"
                "- domain : (optional) Domain tag for indexing (default: 'ONTAP')\n"
                "- team   : (optional) Team tag for indexing (default: 'General')\n\n"
                "Returns the article title, metadata, and full body text."
            ),
            inputSchema={
                "type": "object",
                "required": ["article_id_or_url"],
                "properties": {
                    "article_id_or_url": {
                        "type": "string",
                        "description": "Article ID, partial path, or full kb.netapp.com URL",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Domain tag for indexing (default: ONTAP)",
                    },
                    "team": {
                        "type": "string",
                        "description": "Team tag for indexing (default: General)",
                    },
                },
            },
        ),

        # ---- URL fetch (pass-through, same as v1) -------------------------
        Tool(
            name="kb_fetch_url",
            description=(
                "Fetch and parse any kb.netapp.com URL.\n\n"
                "Useful for following links from case notes or search results "
                "that point to KB pages other than standard articles "
                "(e.g. how-to guides, release notes, category pages).\n\n"
                "Parameters:\n"
                "- url : (required) A kb.netapp.com or netapp.com URL\n\n"
                "Returns the page title and clean body text."
            ),
            inputSchema={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "kb.netapp.com or netapp.com URL to fetch",
                    },
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch tool calls to the appropriate handler."""

    # --- kb_check_auth ---
    if name == "kb_check_auth":
        return _json_result(auth_manager.get_cookie_status())

    # --- kb_refresh_login ---
    if name == "kb_refresh_login":
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, login_helper.do_login)
        if success:
            return _json_result({"success": True, "status": auth_manager.get_cookie_status()})
        return _json_result({"success": False, "message": "Login failed or timed out."})

    # All remaining tools require valid auth
    auth_error = _ensure_auth()
    if auth_error:
        return [TextContent(type="text", text=auth_error)]

    # Wait for model warm-up to finish (blocks at most ~15s on cold start;
    # instant on subsequent calls).
    _warmup_done.wait(timeout=30)

    # --- kb_semantic_search ---
    if name == "kb_semantic_search":
        query = arguments.get("query", "").strip()
        if not query:
            return [TextContent(type="text", text="Error: 'query' parameter is required.")]
        result = kbc.semantic_search(
            query=query,
            domain=arguments.get("domain"),
            team=arguments.get("team"),
            valid_after=arguments.get("valid_after"),
            limit=min(int(arguments.get("limit", 10)), 25),
        )
        return _json_result(result)

    # --- kb_keyword_lookup ---
    if name == "kb_keyword_lookup":
        term = arguments.get("term", "").strip()
        if not term:
            return [TextContent(type="text", text="Error: 'term' parameter is required.")]
        result = kbc.keyword_lookup(
            term=term,
            domain=arguments.get("domain"),
            team=arguments.get("team"),
            valid_after=arguments.get("valid_after"),
            limit=min(int(arguments.get("limit", 10)), 25),
        )
        return _json_result(result)

    # --- kb_get_article ---
    if name == "kb_get_article":
        id_or_url = arguments.get("article_id_or_url", "").strip()
        if not id_or_url:
            return [TextContent(type="text", text="Error: 'article_id_or_url' is required.")]
        result = kbc.get_article(
            article_id_or_url=id_or_url,
            domain=arguments.get("domain", "ONTAP"),
            team=arguments.get("team", "General"),
        )
        return _json_result(result)

    # --- kb_fetch_url ---
    if name == "kb_fetch_url":
        url = arguments.get("url", "").strip()
        if not url:
            return [TextContent(type="text", text="Error: 'url' parameter is required.")]
        result = kbc.fetch_kb_url(url)
        return _json_result(result)

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point (identical to v1)
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
