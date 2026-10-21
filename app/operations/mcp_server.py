"""Minimal stdio MCP read-only bridge. Explicit database and property scope required.

Run: python -m app.operations.mcp_server. No HTTP listener or background scheduler.
"""
import json
import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.operations.tools import OpsTools

TOOLS = [
    {"name": "get_property_facts", "description": "Read non-secret property facts with evidence IDs.",
     "inputSchema": {"type": "object", "properties": {"property_id": {"type": "integer", "minimum": 1}}, "required": ["property_id"], "additionalProperties": False}},
    {"name": "list_arrivals", "description": "Read scoped arrivals, inclusive local dates, maximum 31-day span.",
     "inputSchema": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}, "required": ["start", "end"], "additionalProperties": False}},
    {"name": "list_overdue_tasks", "description": "Read scoped unfinished cleaning tasks at a local timestamp.",
     "inputSchema": {"type": "object", "properties": {"as_of": {"type": "string"}}, "required": ["as_of"], "additionalProperties": False}},
    {"name": "get_reconciliation", "description": "Read scoped month totals and missing amount counts, never treat missing as zero.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def engine_from_environment():
    url = os.environ.get("OPS_TOOL_DATABASE_URL", "")
    scope = os.environ.get("OPS_TOOL_PROPERTY_IDS", "")
    ids = [int(x) for x in scope.split(",") if x.strip()]
    if not url or not ids or any(i < 1 for i in ids):
        raise ValueError("explicit_read_only_database_and_scope_required")
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        if not path.startswith("/") or "?" in path or "#" in path or not os.path.isfile(path):
            raise ValueError("existing_absolute_sqlite_file_required")
        from urllib.parse import quote
        url = "sqlite:///file:" + quote(path, safe="/") + "?mode=ro&uri=true"
    elif not url.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise ValueError("unsupported_database")
    return create_engine(url), ids


def call(engine, ids, name, args):
    definitions = {t["name"]: t for t in TOOLS}
    if name not in definitions or not isinstance(args, dict):
        raise ValueError("unknown_tool_or_arguments")
    schema = definitions[name]["inputSchema"]
    if set(args) != set(schema["required"] if "required" in schema else []):
        raise ValueError("invalid_arguments")
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SET TRANSACTION READ ONLY"))
        else:
            conn.execute(text("PRAGMA query_only=ON"))
        with Session(bind=conn) as db:
            service = OpsTools(db, ids)
            methods = {"get_property_facts": service.property_facts, "list_arrivals": service.arrivals,
                       "list_overdue_tasks": service.overdue_tasks, "get_reconciliation": service.reconciliation}
            return methods[name](**args)


def serve(engine, ids, source, destination):
    for line in source:
        if len(line) > 65536:
            continue
        request_id = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError()
            request_id = req.get("id")
            if request_id is None:
                continue
            method = req.get("method")
            if method == "initialize":
                proposed = req.get("params", {}).get("protocolVersion")
                version = proposed if proposed in ("2024-11-05", "2025-03-26", "2025-06-18") else "2025-03-26"
                result = {"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "oceano-ops-readonly", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                p = req.get("params", {})
                try:
                    value = call(engine, ids, p.get("name"), p.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
                except Exception:
                    result = {"content": [{"type": "text", "text": "Tool failed or scope denied; no changes made"}], "isError": True}
            else:
                destination.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}) + "\n")
                destination.flush()
                continue
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid request"}}
        destination.write(json.dumps(response) + "\n")
        destination.flush()


if __name__ == "__main__":
    try:
        eng, ids = engine_from_environment()
    except Exception:
        sys.stderr.write("Explicit existing database and property scope required; startup refused.\n")
        raise SystemExit(2)
    try:
        serve(eng, ids, sys.stdin, sys.stdout)
    finally:
        eng.dispose()
