"""MCP Server Connection Adapter: optional client to a remote MCP server."""
import asyncio, json, os

URL = os.getenv("MCP_SERVER_URL", "")
TOKEN = os.getenv("MCP_AUTH_TOKEN", "")


def enabled() -> bool:
    return bool(URL)


async def _session_call(fn):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else None
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def list_tools() -> str:
    """Tools offered by configured remote MCP servers (none unless configured)."""
    if not enabled():
        return ""
    async def go(s):
        res = await s.list_tools()
        return "\n".join(f"- {t.name}: {(t.description or '')[:100]}" for t in res.tools)
    try:
        return asyncio.run(_session_call(go))
    except Exception as e:
        return f"(MCP unavailable: {e})"


def call(spec: str) -> str:
    """spec: JSON string {"name": "...", "arguments": {...}}"""
    if not enabled():
        return "MCP is not configured"
    try:
        req = json.loads(spec)
        name, args = req["name"], req.get("arguments", {})
    except Exception:
        return 'mcp input must be JSON: {"name": "...", "arguments": {...}}'
    async def go(s):
        res = await s.call_tool(name, args)
        return "\n".join(getattr(c, "text", str(c)) for c in res.content)
    try:
        out = asyncio.run(_session_call(go))
    except Exception as e:
        return f"[mcp error: {e}]"
    return out[:2000]
