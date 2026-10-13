"""LLM mapping agent: constrained tool loop with strict validation of everything the model returns."""
from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import ValidationError

from ..config import Settings
from ..schemas import AgentProposal
from ..services import engine as E
from .openrouter import AIServiceError, parse_json_response
from .tools import SUBMIT_TOOL, TERMINAL_TOOL, TOOL_DEFS, ToolBox, ToolRejected, to_message

Emit = Callable[..., None]


class AgentError(AIServiceError):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.details = code, details


SYSTEM = """You are the planning agent inside a data-migration review workbench. Propose how to migrate ONE bounded dataset
from the source schema (legacy_customers) to the target schema (accounts).

Rules:
- You may ONLY use the provided tools: read the schemas, list supported transformations, profile or sample source values, and
  validate a candidate mapping. You cannot execute, approve, write, or run code. A human reviews and must approve your plan.
- Use ONLY supported transformation rules (call list_supported_transformations). Never invent a rule, parameter, code, SQL or shell command.
- Be efficient with rounds: call multiple tools in parallel where helpful (e.g. read schemas together, profile ambiguous fields together).
  Profile only non-trivial ambiguous fields (such as dates, phone_no, balance, status_cd). Do NOT profile every trivial field one by one across separate turns.
- Call validate_candidate_mapping with your full candidate at least once and fix what you can.
- Phone numbers: Never silently assume US (+1) or India (+91) default country code for unqualified numbers. Numbers with + or 00 retain their existing country code. If national numbers lack a country code and no default region is established by the source or reviewer, leave default_region as null/unresolved, flag it in risks and ask the reviewer.
- Missing business data: Never silently assign unverified constants (such as tier='standard' or marketing_opt_in=false) as established facts. Absent consent does not prove refusal or agreement. Missing fields must be flagged with policy='unresolved' requiring reviewer approval, with semantic consequences documented in risks and questions. List fields you are unsure about in `uncertain`.
- Map every target field and include every required target field.
- Finish by calling submit_plan exactly once promptly after validation."""


def _user_prompt(base: dict | None, answers: list[dict]) -> str:
    if not base:
        return "Create the first migration plan proposal."
    return ("Revise this plan. Keep what is already right and apply the reviewer's answers.\n"
            f"Current mappings: {json.dumps(E.canon(base['mappings']))}\n"
            f"Reviewer answers: {json.dumps(answers) if answers else '(none yet)'}")


def _validate(raw: Any) -> dict:
    """Pydantic contract + business rules. Returns the normalised proposal or raises AgentError."""
    try:
        prop = AgentProposal.model_validate(raw)
    except ValidationError as exc:
        raise AgentError("ai_invalid_output", "The model's plan did not match the required structure.", exc.errors(include_url=False, include_context=False)[:8])
    data = prop.model_dump()
    norm = E.normalize_proposal(data)
    for u in data["uncertain"]:
        norm["incompatibilities"].append({"kind": "uncertain_classification", "field": u["field"], "text": u["reason"]})
    if not norm["mappings"]:
        raise AgentError("ai_invalid_mapping", "None of the model's mappings used supported rules and fields.", {"dropped": norm["dropped"]})
    v = E.validate_plan(norm["mappings"])
    if not v["ok"]:
        raise AgentError("ai_invalid_mapping", "The model's mapping is not valid for this schema.", {"issues": [i for i in v["issues"] if i["level"] == "error"], "dropped": norm["dropped"]})
    return norm


def run_agent(client, records: list[dict], settings: Settings, *, base: dict | None = None, answers: list[dict] | None = None, emit: Emit | None = None) -> dict:
    """Returns {"proposal": normalised dict, "model": str, "trace": [...]}. Raises AIServiceError subclasses on any failure."""
    emit = emit or (lambda *a, **k: None)
    box = ToolBox(records)
    messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _user_prompt(base, answers or [])}]
    tools = TOOL_DEFS + [SUBMIT_TOOL]
    trace: list[dict] = []
    model = None
    for rnd in range(1, settings.agent_max_rounds + 1):
        out = client.chat(messages, tools=tools, tool_choice="auto")
        msg, model = out["message"], out.get("model") or model
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content"), **({"tool_calls": calls} if calls else {})})
        if not calls:  # plain-text answer: accept JSON, else ask again
            try:
                raw = parse_json_response(msg.get("content") or "")
                norm = _validate(raw)
                trace.append({"round": rnd, "tool": "final_text_json", "outcome": "ok", "summary": f'{len(norm["mappings"])} mappings'})
                emit("info", "agent.final", round=rnd, via="text", mappings=len(norm["mappings"]))
                return {"proposal": norm, "model": model, "trace": trace}
            except (ValueError, AgentError) as exc:
                messages.append({"role": "user", "content": "That was not a valid plan (" + str(exc)[:300] + "). Call submit_plan with a valid plan."})
                trace.append({"round": rnd, "tool": "final_text_json", "outcome": "rejected", "summary": str(exc)[:160]})
                emit("warning", "agent.invalid_final", round=rnd, error=str(exc)[:160])
                continue
        for call in calls:
            name = (call.get("function") or {}).get("name", "")
            cid = call.get("id", "")
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                args = None
                reply = {"error": f"Arguments were not valid JSON: {exc}"}
                trace.append({"round": rnd, "tool": name, "outcome": "error", "summary": "invalid JSON arguments"})
                emit("warning", "agent.tool_error", tool=name, round=rnd, error="invalid_json_arguments")
                messages.append({"role": "tool", "tool_call_id": cid, "content": to_message(reply)})
                continue
            if name == TERMINAL_TOOL:
                try:
                    norm = _validate(args)
                except AgentError as exc:
                    reply = {"error": exc.args[0], "details": exc.details}
                    trace.append({"round": rnd, "tool": name, "outcome": "rejected", "summary": exc.code})
                    emit("warning", "agent.submit_rejected", round=rnd, code=exc.code)
                    messages.append({"role": "tool", "tool_call_id": cid, "content": to_message(reply)})
                    continue
                trace.append({"round": rnd, "tool": name, "outcome": "ok", "summary": f'{len(norm["mappings"])} mappings, {len(norm["dropped"])} dropped'})
                emit("info", "agent.final", round=rnd, via="submit_plan", mappings=len(norm["mappings"]), dropped=len(norm["dropped"]))
                return {"proposal": norm, "model": model, "trace": trace}
            try:
                result, summary = box.call(name, args)
                trace.append({"round": rnd, "tool": name, "args": {k: (v if k != "mappings" else f"{len(v) if isinstance(v, list) else 0} items") for k, v in args.items()}, "outcome": "ok", "summary": summary})
                emit("info", "agent.tool", tool=name, round=rnd, outcome="ok", summary=summary)
                messages.append({"role": "tool", "tool_call_id": cid, "content": to_message(result)})
            except ToolRejected as exc:
                trace.append({"round": rnd, "tool": name, "outcome": "rejected", "summary": str(exc)[:160]})
                emit("warning", "agent.tool_rejected", tool=name, round=rnd, reason=str(exc)[:160])
                messages.append({"role": "tool", "tool_call_id": cid, "content": to_message({"error": str(exc)})})
    raise AgentError("ai_no_result", f"The model did not submit a plan within {settings.agent_max_rounds} rounds.", {"trace": trace})
