#!/usr/bin/env python3
"""
KB NetApp MCP Server
====================
An MCP server that provides tools to search and read articles on kb.netapp.com.

Authentication is handled via session cookies captured by the Playwright-based
login helper (Set-KBCookies.ps1 / login_helper.py). Cookies are stored
securely in Windows Credential Manager via keyring.

PORTABLE: All configuration is via environment variables:
  KB_USERNAME : Your NetApp email/username (default: current system user)

Setup:
  1. pip install -r requirements.txt
  2. .\\Set-KBCookies.ps1         ← log in once to capture cookies
  4. Add to your mcp-config.json (see README.md)
  5. /restart in Copilot
"""

import asyncio
import json
import logging
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import auth_manager
import kb_client
import login_helper

# Suppress noisy logs; auth errors are surfaced via tool return values
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("kb-mcp")

server = Server("kb-netapp-mcp")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _json_result(data: dict | list) -> list[TextContent]:
    """Wrap a dict/list as pretty-printed JSON TextContent."""
    return [TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False))]


def _ensure_auth() -> Optional[str]:
    """
    Return an error message string if cookies are missing or expired,
    otherwise return None (auth OK).
    """
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
        Tool(
            name="kb_search",
            description=(
                "Search for KB articles on kb.netapp.com.\n\n"
                "Parameters:\n"
                "- query    : (required) Free-text search query\n"
                "- product  : (optional) Product filter, e.g. 'ONTAP', 'StorageGRID'\n"
                "- category : (optional) Category filter\n"
                "- limit    : (optional) Max results to return (default: 10, max: 25)\n\n"
                "Returns a list of matching articles with title, URL, and snippet."
            ),
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search query",
                    },
                    "product": {
                        "type": "string",
                        "description": "Product filter (e.g. 'ONTAP', 'StorageGRID')",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category filter",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of results (default: 10)",
                        "minimum": 1,
                        "maximum": 25,
                    },
                },
            },
        ),
        Tool(
            name="kb_get_article",
            description=(
                "Fetch the full content of a KB article.\n\n"
                "Parameters:\n"
                "- article_id_or_url : (required) One of:\n"
                "    • Numeric article ID:   e.g. '1234567'\n"
                "    • Partial path:         e.g. 'on-prem/ontap/article/1234567'\n"
                "    • Full URL:             e.g. 'https://kb.netapp.com/...'\n\n"
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
                },
            },
        ),
        Tool(
            name="kb_fetch_url",
            description=(
                "Fetch and parse any kb.netapp.com URL.\n\n"
                "Useful for following links from case notes or search results "
                "that point to KB pages other than standard articles "
                "(e.g. how-to guides, release notes, etc.).\n\n"
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
        status = auth_manager.get_cookie_status()
        return _json_result(status)

    # --- kb_refresh_login ---
    if name == "kb_refresh_login":
        # Run the blocking Playwright login in a thread so we don't block the event loop
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, login_helper.do_login)
        if success:
            status = auth_manager.get_cookie_status()
            return _json_result({"success": True, "status": status})
        return _json_result({"success": False, "message": "Login failed or timed out. Please try again."})

    # All remaining tools require valid auth
    auth_error = _ensure_auth()
    if auth_error:
        return [TextContent(type="text", text=auth_error)]

    # --- kb_search ---
    if name == "kb_search":
        query = arguments.get("query", "").strip()
        if not query:
            return [TextContent(type="text", text="Error: 'query' parameter is required.")]

        result = kb_client.search_kb(
            query=query,
            product=arguments.get("product"),
            category=arguments.get("category"),
            limit=min(int(arguments.get("limit", 10)), 25),
        )
        return _json_result(result)

    # --- kb_get_article ---
    if name == "kb_get_article":
        id_or_url = arguments.get("article_id_or_url", "").strip()
        if not id_or_url:
            return [TextContent(type="text", text="Error: 'article_id_or_url' parameter is required.")]

        result = kb_client.get_article(id_or_url)
        return _json_result(result)

    # --- kb_fetch_url ---
    if name == "kb_fetch_url":
        url = arguments.get("url", "").strip()
        if not url:
            return [TextContent(type="text", text="Error: 'url' parameter is required.")]

        result = kb_client.fetch_kb_url(url)
        return _json_result(result)

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
