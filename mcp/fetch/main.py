import os
import httpx
import uvicorn
from fastmcp import FastMCP
import fastmcp
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from fastmcp.utilities.logging import configure_logging
from markdownify import markdownify as md
from bs4 import BeautifulSoup

# MCP metadata for x-mcp-tag header
MCP_TAG = f"framework=fastmcp;version={fastmcp.__version__};server=fetch;api=web-fetch"

LOGLVL = os.environ.get("LOGLVL", "INFO")
configure_logging(level=LOGLVL)

mcp = FastMCP("fetch-mcp-server")

# User agent for requests
USER_AGENT = "Mozilla/5.0 (compatible; MCPFetchBot/1.0)"


@mcp.tool()
async def fetch(
    url: str,
    max_length: int = 5000,
    start_index: int = 0,
    raw: bool = False
) -> dict:
    """Fetch web content from a URL and convert to markdown.

    Args:
        url: The URL to fetch content from
        max_length: Maximum length of returned content (default 5000)
        start_index: Starting character position for extraction (default 0)
        raw: If true, return raw HTML without markdown conversion (default false)

    Returns:
        Dictionary with url, title, and content fields
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(
                url,
                headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()

            html_content = response.text

            if raw:
                content = html_content
            else:
                # Parse HTML and convert to markdown
                soup = BeautifulSoup(html_content, "html.parser")

                # Remove script and style elements
                for element in soup(["script", "style", "nav", "footer", "header"]):
                    element.decompose()

                # Get title
                title = soup.title.string if soup.title else "No title"

                # Convert to markdown
                content = md(str(soup.body) if soup.body else str(soup))

            # Apply start_index and max_length
            content = content[start_index:start_index + max_length]

            return {
                "url": str(response.url),
                "title": title if not raw else "Raw HTML",
                "content": content,
                "content_length": len(content),
                "truncated": len(html_content) > (start_index + max_length)
            }

    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP error: {e.response.status_code}", "url": url}
    except httpx.RequestError as e:
        return {"error": f"Request failed: {str(e)}", "url": url}
    except Exception as e:
        return {"error": f"Failed to fetch: {str(e)}", "url": url}


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({
        "status": "healthy",
        "service": "fetch-mcp-server",
        "version": "1.0.0"
    })


class MCPTagMiddleware(BaseHTTPMiddleware):
    """Middleware to add x-mcp-tag header to all responses."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["x-mcp-tag"] = MCP_TAG
        return response


mcp_app = mcp.http_app()

mcp_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["Content-Type", "Mcp-Session-Id", "Authorization", "mcp-protocol-version", "x-agent-tag", "x-pxgw-tag"],
    expose_headers=["Mcp-Session-Id", "x-mcp-tag", "x-pxgw-tag"],
)

mcp_app.add_middleware(MCPTagMiddleware)

if __name__ == "__main__":
    uvicorn.run(mcp_app, host="0.0.0.0", port=8000, log_level="debug")
