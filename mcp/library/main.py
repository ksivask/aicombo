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
from typing import Optional

# MCP metadata for x-mcp-tag header
MCP_TAG = f"framework=fastmcp;version={fastmcp.__version__};server=library;api=open-library"

LOGLVL = os.environ.get("LOGLVL", "INFO")
configure_logging(level=LOGLVL)

mcp = FastMCP("library-mcp-server")

OPEN_LIBRARY_BASE = "https://openlibrary.org"
COVERS_BASE = "https://covers.openlibrary.org"


@mcp.tool()
async def get_book_by_title(title: str, limit: int = 5) -> dict:
    """Search for books by title using Open Library API.

    Args:
        title: The book title to search for
        limit: Maximum number of results to return (default 5)

    Returns:
        Dictionary with search results including title, author, year, and cover URLs
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{OPEN_LIBRARY_BASE}/search.json",
                params={"title": title, "limit": limit}
            )
            response.raise_for_status()
            data = response.json()

            books = []
            for doc in data.get("docs", [])[:limit]:
                book = {
                    "title": doc.get("title"),
                    "authors": doc.get("author_name", []),
                    "first_publish_year": doc.get("first_publish_year"),
                    "isbn": doc.get("isbn", [])[:3] if doc.get("isbn") else [],
                    "key": doc.get("key"),
                    "cover_id": doc.get("cover_i"),
                }
                if book["cover_id"]:
                    book["cover_url"] = f"{COVERS_BASE}/b/id/{book['cover_id']}-M.jpg"
                books.append(book)

            return {
                "query": title,
                "total_results": data.get("numFound", 0),
                "books": books
            }

    except Exception as e:
        return {"error": f"Search failed: {str(e)}", "query": title}


@mcp.tool()
async def get_authors_by_name(name: str, limit: int = 5) -> dict:
    """Search for authors by name using Open Library API.

    Args:
        name: The author name to search for
        limit: Maximum number of results to return (default 5)

    Returns:
        Dictionary with author information including key, name, birth date, and work count
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{OPEN_LIBRARY_BASE}/search/authors.json",
                params={"q": name, "limit": limit}
            )
            response.raise_for_status()
            data = response.json()

            authors = []
            for doc in data.get("docs", [])[:limit]:
                author = {
                    "key": doc.get("key"),
                    "name": doc.get("name"),
                    "birth_date": doc.get("birth_date"),
                    "death_date": doc.get("death_date"),
                    "work_count": doc.get("work_count", 0),
                    "top_work": doc.get("top_work"),
                    "alternate_names": doc.get("alternate_names", [])[:3]
                }
                authors.append(author)

            return {
                "query": name,
                "total_results": data.get("numFound", 0),
                "authors": authors
            }

    except Exception as e:
        return {"error": f"Search failed: {str(e)}", "query": name}


@mcp.tool()
async def get_book_by_id(
    identifier: str,
    id_type: str = "isbn"
) -> dict:
    """Get detailed book information by identifier.

    Args:
        identifier: The book identifier (ISBN, OCLC, LCCN, or OLID)
        id_type: Type of identifier - 'isbn', 'oclc', 'lccn', or 'olid' (default 'isbn')

    Returns:
        Dictionary with detailed book information
    """
    try:
        # Normalize id_type
        id_type = id_type.lower()
        if id_type not in ["isbn", "oclc", "lccn", "olid"]:
            return {"error": f"Invalid id_type: {id_type}. Use isbn, oclc, lccn, or olid"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            if id_type == "olid":
                url = f"{OPEN_LIBRARY_BASE}/books/{identifier}.json"
            else:
                url = f"{OPEN_LIBRARY_BASE}/api/books"
                bibkey = f"{id_type.upper()}:{identifier}"
                response = await client.get(
                    url,
                    params={"bibkeys": bibkey, "format": "json", "jscmd": "data"}
                )
                response.raise_for_status()
                data = response.json()

                if not data:
                    return {"error": f"Book not found with {id_type}: {identifier}"}

                book_data = data.get(bibkey, {})
                return {
                    "identifier": identifier,
                    "id_type": id_type,
                    "title": book_data.get("title"),
                    "authors": [a.get("name") for a in book_data.get("authors", [])],
                    "publishers": [p.get("name") for p in book_data.get("publishers", [])],
                    "publish_date": book_data.get("publish_date"),
                    "number_of_pages": book_data.get("number_of_pages"),
                    "subjects": [s.get("name") for s in book_data.get("subjects", [])][:5],
                    "cover": book_data.get("cover", {}),
                    "url": book_data.get("url")
                }

            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

            return {
                "identifier": identifier,
                "id_type": id_type,
                "title": data.get("title"),
                "key": data.get("key"),
                "authors": data.get("authors", []),
                "publishers": data.get("publishers", []),
                "publish_date": data.get("publish_date"),
                "number_of_pages": data.get("number_of_pages")
            }

    except Exception as e:
        return {"error": f"Lookup failed: {str(e)}", "identifier": identifier}


@mcp.tool()
async def get_book_cover(
    identifier: str,
    id_type: str = "isbn",
    size: str = "M"
) -> dict:
    """Get book cover image URL.

    Args:
        identifier: The book identifier
        id_type: Type - 'isbn', 'oclc', 'lccn', 'olid', or 'id' (cover ID)
        size: Image size - 'S' (small), 'M' (medium), or 'L' (large)

    Returns:
        Dictionary with cover URL
    """
    size = size.upper()
    if size not in ["S", "M", "L"]:
        size = "M"

    id_type = id_type.lower()
    type_map = {"isbn": "isbn", "oclc": "oclc", "lccn": "lccn", "olid": "olid", "id": "id"}

    if id_type not in type_map:
        return {"error": f"Invalid id_type: {id_type}"}

    cover_url = f"{COVERS_BASE}/b/{type_map[id_type]}/{identifier}-{size}.jpg"

    return {
        "identifier": identifier,
        "id_type": id_type,
        "size": size,
        "cover_url": cover_url
    }


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({
        "status": "healthy",
        "service": "library-mcp-server",
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
