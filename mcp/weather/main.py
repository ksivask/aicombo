import os
import httpx
import uvicorn
from fastmcp import FastMCP
import fastmcp
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount
from fastmcp.utilities.logging import configure_logging

# MCP metadata for x-mcp-tag header
MCP_TAG = f"framework=fastmcp;version={fastmcp.__version__};server=weather;api=open-meteo"

LOGLVL = os.environ.get("LOGLVL", "INFO")
# Set to DEBUG to see detailed protocol and server info
configure_logging(level=LOGLVL)

mcp = FastMCP("weather-mcp-server")

@mcp.tool()
async def get_weather(city: str) -> dict:
    """Get weather forecast for a city using Open-Meteo API."""
    # First, geocode the city to get coordinates
    geocode_url = "https://geocoding-api.open-meteo.com/v1/search"
    async with httpx.AsyncClient() as client:
        geo_response = await client.get(geocode_url, params={"name": city, "count": 1})
        geo_data = geo_response.json()

        if not geo_data.get("results"):
            return {"error": f"City '{city}' not found"}

        location = geo_data["results"][0]
        lat = location["latitude"]
        lon = location["longitude"]

        # Get weather data
        weather_url = "https://api.open-meteo.com/v1/forecast"
        weather_response = await client.get(weather_url, params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "timezone": "auto"
        })
        weather_data = weather_response.json()

        current = weather_data.get("current", {})
        return {
            "city": location["name"],
            "country": location.get("country", ""),
            "temperature_c": current.get("temperature_2m"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "weather_code": current.get("weather_code")
        }

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({
        "status": "healthy",
        "service": "weather-mcp-server",
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
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["Content-Type", "Mcp-Session-Id", "Authorization", "mcp-protocol-version", "x-agent-tag", "x-pxgw-tag"],
    expose_headers=["Mcp-Session-Id", "x-mcp-tag", "x-pxgw-tag"],
)

# Add middleware to the FastMCP app
mcp_app.add_middleware(MCPTagMiddleware)

if __name__ == "__main__":
    uvicorn.run(mcp_app, host="0.0.0.0", port=8000, log_level="debug")
    #mcp.run(transport="http", host="0.0.0.0", port=8000, path="/mcp", log_level="debug")

# parent_app = Starlette(
#     routes=[
#         Mount("/", app=mcp_app)
#     ]
# )
    #uvicorn.run(parent_app, host="0.0.0.0", port=8000, log_level="debug")
