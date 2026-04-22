import os
import httpx
import uvicorn
from bs4 import BeautifulSoup
from fastmcp import FastMCP
import fastmcp
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from fastmcp.utilities.logging import configure_logging

# MCP metadata for x-mcp-tag header
MCP_TAG = f"framework=fastmcp;version={fastmcp.__version__};server=news;api=google-news-rss"

LOGLVL = os.environ.get("LOGLVL", "INFO")
# Set to DEBUG to see detailed protocol and server info
configure_logging(level=LOGLVL)

mcp = FastMCP("news-mcp-server")

@mcp.tool()
async def get_news(topic: str) -> dict:
    """Get latest news headlines for a topic using Google News RSS."""
    rss_url = f"https://news.google.com/rss/search?q={topic}&hl=en-US&gl=US&ceid=US:en"

    async with httpx.AsyncClient() as client:
        response = await client.get(rss_url, follow_redirects=True)

        if response.status_code != 200:
            return {"error": f"Failed to fetch news: {response.status_code}"}

        #logger.debug("news: %s", response.text)
        soup = BeautifulSoup(response.text, "xml")
        items = soup.find_all("item", limit=5)
        
        articles = []
        for item in items:
            title = item.find("title")
            pub_date = item.find("pubDate")
            source = item.find("source")

            articles.append({
                "title": title.text if title else "No title",
                "published": pub_date.text if pub_date else "Unknown",
                "source": source.text if source else "Unknown"
            })

        return {
            "topic": topic,
            "article_count": len(articles),
            "articles": articles
        }

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({
        "status": "healthy",
        "service": "news-mcp-server",
        "version": "1.0.0"
    })

class MCPTagMiddleware(BaseHTTPMiddleware):
    """Middleware to add x-mcp-tag header to all responses."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["x-mcp-tag"] = MCP_TAG
        return response

mcp_app = mcp.http_app()

# Access the underlying Starlette/FastAPI app to add middleware
mcp_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For development; restrict this in production
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["Content-Type", "Mcp-Session-Id", "Authorization", "mcp-protocol-version", "x-agent-tag", "x-pxgw-tag"],
    expose_headers=["Mcp-Session-Id", "x-mcp-tag", "x-pxgw-tag"],
)

# Add middleware to the FastMCP app
mcp_app.add_middleware(MCPTagMiddleware)

if __name__ == "__main__":
    uvicorn.run(mcp_app, host="0.0.0.0", port=8000, log_level="debug")
