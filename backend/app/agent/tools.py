"""The ONLY tools the agent may call. Everything else is rejected by name. No tool can write, execute or approve anything."""
from __future__ import annotations

import json
from typing import Any

from ..services import engine as E

MAX_RESULT_CHARS = 12000


def _fn(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


TOOL_DEFS = [
    _fn("get_source_schema", "Returns the source schema (field names, types, nullability, descriptions)."),
    _fn("get_target_schema", "Returns the target schema (fields, types, required flags, enums, patterns, primary key)."),
    _fn("list_supported_transformations", "Lists the ONLY supported transformation rules and their parameters. Never use a rule that is not listed."),
    _fn("profile_source_field", "Profiles one source field over the sample: blank count, distinct count, top values, top value shapes (digits=9, letters=a/A), and date-order evidence for date fields.",
        {"field": {"type": "string"}}, ["field"]),
    _fn("sample_source_values", "Returns up to 15 raw values of one source field with row numbers.", {"field": {"type": "string"}, "limit": {"type": "integer"}}, ["field"]),
    _fn("validate_candidate_mapping", "Dry-runs a candidate mapping over the sample with the deterministic engine. Returns plan issues, counts, top error codes and up to 3 example failures. Writes nothing.",
        {"mappings": {"type": "array", "items": {"type": "object", "properties": {"target": {"type": "string"}, "source": {"type": ["string", "null"]}, "rule": {"type": "string"}, "params": {"type": "object"}}, "required": ["target", "rule"]}}}, ["mappings"]),
]

SUBMIT_TOOL = _fn(
    "submit_plan", "Submit the final migration plan. Call this exactly once when finished.",
    {
        "summary": {"type": "string"},
        "mappings": {"type": "array", "items": {"type": "object", "properties": {
            "target": {"type": "string"}, "source": {"type": ["string", "null"]}, "rule": {"type": "string"}, "params": {"type": "object"},
            "confidence": {"type": "number"}, "rationale": {"type": "string"}}, "required": ["target", "rule"]}},
        "incompatibilities": {"type": "array", "items": {"type": "object", "properties": {"kind": {"type": "string"}, "field": {"type": ["string", "null"]}, "text": {"type": "string"}}, "required": ["text"]}},
        "risks": {"type": "array", "items": {"type": "object", "properties": {"level": {"type": "string", "enum": ["high", "medium", "low"]}, "field": {"type": ["string", "null"]}, "text": {"type": "string"}}, "required": ["text"]}},
        "questions": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": ["string", "null"]}, "text": {"type": "string"}, "assumption": {"type": ["string", "null"]}}, "required": ["text"]}},
        "uncertain": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "reason": {"type": "string"}}, "required": ["field", "reason"]}},
    }, ["summary", "mappings"])

ALLOWED_TOOLS = {t["function"]["name"] for t in TOOL_DEFS}
TERMINAL_TOOL = "submit_plan"


class ToolRejected(Exception):
    pass


class ToolBox:
    def __init__(self, records: list[dict]):
        self.records = records

    def call(self, name: str, args: dict) -> tuple[Any, str]:
        """Returns (result, one-line summary). Raises ToolRejected for anything outside the allowlist or with bad arguments."""
        if name not in ALLOWED_TOOLS:
            raise ToolRejected(f'Tool "{name}" is not allowed. Allowed tools: {sorted(ALLOWED_TOOLS)} and submit_plan.')
        if not isinstance(args, dict):
            raise ToolRejected("Tool arguments must be a JSON object.")
        if name == "get_source_schema":
            return E.SOURCE_SCHEMA, f'{len(E.SOURCE_SCHEMA["fields"])} fields'
        if name == "get_target_schema":
            return E.TARGET_SCHEMA, f'{len(E.TARGET_SCHEMA["fields"])} fields'
        if name == "list_supported_transformations":
            return E.describe_rules(), f"{len(E.RULES)} rules"
        if name in ("profile_source_field", "sample_source_values"):
            field = str(args.get("field", ""))
            if field not in E.SF:
                raise ToolRejected(f'Unknown source field "{field}".')
            if name == "profile_source_field":
                p = E.profile_field(self.records, field)
                if field in ("dob", "signup_date"):
                    p["date_order_evidence"] = E.date_order_evidence(self.records, field)
                return p, f'{p["distinct"]} distinct, {p["blank"]} blank'
            n = max(1, min(15, int(args.get("limit") or 10)))
            return [{"row": i + 1, "value": r.get(field)} for i, r in enumerate(self.records[:n])], f"{n} values"
        # validate_candidate_mapping
        norm = E.normalize_proposal({"mappings": args.get("mappings")})
        res = E.dry_run(norm["mappings"], self.records)
        if "error" in res:
            return {"ok": False, "issues": (res.get("issues") or [])[:10], "dropped": norm["dropped"]}, "invalid: " + res["message"]
        out = {"ok": True, "counts": res["counts"], "top_errors": res["error_summary"][:10], "dropped": norm["dropped"],
               "examples": [{"row": r["row"], "errors": [{"field": e["field"], "code": e["code"], "source_value": e.get("source_value")} for e in r["errors"]]} for r in res["rejected"][:3]]}
        return out, f'{res["counts"]["accepted"]} of {res["counts"]["source"]} accepted'


def to_message(result: Any) -> str:
    s = json.dumps(result, default=str)
    return s if len(s) <= MAX_RESULT_CHARS else s[:MAX_RESULT_CHARS] + '..."[truncated]'
