"""FastAPI backend for the Debug Cockpit.

Run with:
    uvicorn cockpit.api.main:app --reload --port 8080

Or from cockpit/api directory:
    uvicorn main:app --reload --port 8080
"""

import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from services.postgres import ALLOWED_TABLES, postgres_service


class CustomJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles PostgreSQL types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


class CustomJSONResponse(JSONResponse):
    """JSON response that uses custom encoder."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            cls=CustomJSONEncoder,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    await postgres_service.connect()
    yield
    await postgres_service.disconnect()


app = FastAPI(
    title="Debug Cockpit API",
    description="Backend API for the Graph-RAG Debug Cockpit",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=CustomJSONResponse,
)

# CORS for Angular frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://127.0.0.1:4200"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/tables")
async def list_tables() -> list[dict[str, Any]]:
    """List available tables with row counts."""
    return await postgres_service.get_tables()


@app.get("/api/tables/{table_name}")
async def get_table_data(
    table_name: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500, alias="pageSize"),
) -> dict[str, Any]:
    """Get paginated table data."""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    try:
        return await postgres_service.get_table_data(table_name, page, page_size)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/tables/{table_name}/schema")
async def get_table_schema(table_name: str) -> list[dict[str, Any]]:
    """Get column definitions for a table."""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    try:
        return await postgres_service.get_table_schema(table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
