"""FastAPI app entrypoint for aiplay harness."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import api
from audit_tail import AuditTail


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start audit tail
    container_name = os.environ.get("AGW_CONTAINER_NAME", "agentgateway")
    tail = AuditTail(container_name=container_name)
    tail.start()
    api.AUDIT_TAIL = tail

    yield

    # Shutdown — nothing to explicitly close; subprocess exits with process
    pass


app = FastAPI(title="aiplay — cidgar Harness C", lifespan=lifespan)
app.include_router(api.router)

# Serve frontend static files from /app/frontend/
frontend_dir = Path("/app/frontend")
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
