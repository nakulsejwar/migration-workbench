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

A deterministic BASELINE plan is provided. Treat it as your starting point: keep every baseline mapping unless the profile
evidence clearly shows it is wrong, and say why in that mapping's rationale. Your value is spotting what the heuristics miss
(unusual formats, ambiguous values, risky assumptions), not rebuilding what already works.

Rules:
- You may ONLY use the provided tools: read the schemas, list supported transformations, profile or sample source values, and
  validate a candidate mapping. You cannot execute, approve, write, or run code. A human reviews and must approve your plan.
- Use ONLY supported transformation rules (call list_supported_transformations). Never invent a rule, parameter, code, SQL or shell command.
- Be efficient: call multiple tools in parallel. Do NOT profile every trivial field one by one.
- Call validate_candidate_mapping with your full candidate at least once. A candidate that accepts 0 rows is wrong: fix it.
- Never invent value mappings. A status code that is not clearly the first letter of a target enum value (e.g. 'X') must NOT be
  mapped; list it as a risk and ask the reviewer. Such rows are quarantined, which is the safe outcome.
- Phone numbers: numbers with + or 00 keep their country code. For national numbers use the baseline default region and flag it
  as a risk plus a question with an `assumption`. Never leave default_region empty: that rejects every local number.
- Fields with no source (tier, marketing_opt_in): use rule default_value with the baseline value and NO policy field. Add a HIGH
  risk saying the value is a proposed business default, and a question whose `assumption` is that value. The reviewer decides
  at approval time. Do not use policy='unresolved': it rejects every row and hides all real data problems.
- Reviewer answers, when provided, override the baseline and your own judgment.
- Map every target field. Every question you ask must carry an `assumption`.
- Finish by calling submit_plan exactly once promptly after validation. Reply with tool calls only, never prose."""


def _user_prompt(base: dict | None, answers: list[dict], baseline: dict) -> str:
    bl = f"BASELINE plan (start from this): {json.dumps(E.canon(baseline['mappings']))}\n"
    if not base:
        return bl + "Create the first migration plan proposal."
    return (bl + "Revise this plan. Keep what is already right and apply the reviewer's answers.\n"
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


# ---------------------------------------------------------------------------------------------------------------------
# Deterministic hardening: whatever the model returns is reconciled with the baseline and the reviewer's answers so the
# plan can pass the same approval gate the baseline passes. The model can improve a plan; it can no longer break it.
# ---------------------------------------------------------------------------------------------------------------------
_BUSINESS_FIELDS = ("tier", "marketing_opt_in")
_BAD_POLICIES = ("unresolved", "quarantine", "preserve_unknown")


def _accepted(mappings: list[dict], records: list[dict]) -> int:
    res = E.dry_run(mappings, records)
    return -1 if "error" in res else res["counts"]["accepted"]


def _answer_field(a: dict) -> str | None:
    if a.get("field"):
        return a["field"]
    q = (a.get("question") or "").lower()
    for f in _BUSINESS_FIELDS:
        if f in q:
            return f
    if "calling code" in q or "phone" in q or "region" in q:
        return "phone_e164"
    return None


def _to_bool(v: str) -> bool | None:
    s = v.strip().lower()
    return True if s in ("true", "yes", "y", "1", "on") else False if s in ("false", "no", "n", "0", "off") else None


def _apply_answers(by: dict[str, dict], answers: list[dict], notes: list[str]) -> None:
    for a in answers:
        field, val = _answer_field(a), str(a.get("answer") or "").strip()
        m = by.get(field or "")
        if not m or not val:
            continue
        if field == "tier" and val.lower() in E.TF["tier"]["enum"]:
            params = {"value": val.lower()}
        elif field == "marketing_opt_in" and _to_bool(val) is not None:
            params = {"value": _to_bool(val)}
        elif field == "phone_e164":
            code = val.upper().lstrip("+")
            params = {"default_region": code} if len(code) == 2 and code.isalpha() else {"default_cc": code}
            if E._param_error("normalize_phone", params):
                notes.append(f'Reviewer answer "{val}" for phone region was not a valid region and was ignored.')
                continue
            m["params"] = params
            notes.append(f"Applied reviewer answer for phone region: {val}.")
            continue
        else:
            notes.append(f'Reviewer answer "{val}" for {field} was not a valid value and was ignored.')
            continue
        m.update(rule="default_value", source=None, confidence=1.0, rationale="Set from the reviewer's answer.",
                 params={**params, "policy": "approved_default", "rationale": "Reviewer answer",
                         "semantic_consequences": f"Every migrated account gets {field}={json.dumps(params['value'])}."})
        notes.append(f"Applied reviewer answer for {field}: {val}.")


def _prune_enum_maps(by: dict[str, dict], records: list[dict], risks: list[dict], questions: list[dict], notes: list[str]) -> None:
    """Drop invented code mappings (e.g. X->inactive). Unmapped codes are quarantined, which is the safe outcome."""
    for t, m in by.items():
        enum = (E.TF[t].get("enum") or [])
        if m["rule"] != "map_enum" or not enum:
            continue
        kept, dropped = {}, []
        for k, v in (m["params"].get("map") or {}).items():
            if v in enum and (k == v or (len(k) == 1 and v.startswith(k))):
                kept[k] = v
            else:
                dropped.append(k)
        if dropped and kept:
            m["params"]["map"] = kept
            seen = sum(1 for r in records if str(r.get(m["source"]) or "").strip().lower() in dropped)
            notes.append(f"Removed unsupported {t} mapping(s) {dropped}: the code is not a first-letter match of the target values.")
            risks.append({"level": "medium", "field": t, "text": f"Source code(s) {[d.upper() for d in dropped]} ({seen} record(s)) are not mapped and will be quarantined; confirm their meaning."})
            questions.append({"id": "", "field": t, "text": f"What should source status code(s) {', '.join(d.upper() for d in dropped)} mean? They are quarantined until confirmed.", "assumption": "Quarantine"})


def harden(norm: dict, baseline: dict, records: list[dict], answers: list[dict], settings: Settings) -> dict:
    """Reconcile an agent proposal with the baseline. Returns a new proposal dict in the same shape as normalize_proposal()."""
    mode = getattr(settings, "agent_policy_mode", "propose_defaults")
    notes: list[str] = []
    risks, questions, incompat = list(norm["risks"]), list(norm["questions"]), list(norm["incompatibilities"])
    base_by = {m["target"]: m for m in baseline["mappings"]}
    by = {m["target"]: dict(m, params=dict(m["params"])) for m in norm["mappings"]}

    # 1. every target field must be mapped
    for t, bm in base_by.items():
        if t not in by:
            by[t] = dict(bm, params=dict(bm["params"]), rationale="Filled from the baseline; the model did not map this field.")
            notes.append(f"{t}: filled from baseline.")

    # 2. unresolved business policies and missing phone region reject every row: replace with the baseline proposal
    if mode != "strict":
        for t in _BUSINESS_FIELDS:
            if by[t]["rule"] != "default_value" or by[t]["params"].get("policy") in _BAD_POLICIES:
                by[t] = dict(base_by[t], params=dict(base_by[t]["params"]))
                notes.append(f"{t}: proposed business default {json.dumps(by[t]['params'].get('value'))} used instead of an unresolved policy (flagged for the reviewer).")
        pm = by["phone_e164"]
        if pm["rule"] == "normalize_phone" and not any(E._resolve_phone_region(pm["params"])):
            by["phone_e164"] = dict(base_by["phone_e164"], params=dict(base_by["phone_e164"]["params"]))
            notes.append("phone_e164: baseline default calling code used; the model left the region empty (flagged for the reviewer).")

    _prune_enum_maps(by, records, risks, questions, notes)

    # 3. keep the baseline mapping for any field where it accepts strictly more rows than the model's mapping
    for t in list(by):
        if E.stable(E.canon([by[t]])) == E.stable(E.canon([base_by[t]])):
            continue
        cur = [by[x] for x in by]
        alt = [base_by[t] if x == t else by[x] for x in by]
        if _accepted(alt, records) > _accepted(cur, records):
            by[t] = dict(base_by[t], params=dict(base_by[t]["params"]))
            notes.append(f"{t}: baseline mapping kept because it accepts more records than the model's.")

    # 4. reviewer answers win over everything
    _apply_answers(by, answers, notes)

    # 5. questions must carry an assumption (the UI prefills it); copy missing baseline questions and risks
    bq = {q.get("field"): q for q in baseline["questions"]}
    for q in questions:
        if not q.get("assumption") and q.get("field") in bq:
            q["assumption"] = bq[q["field"]].get("assumption")
    have_q = {q.get("field") for q in questions}
    questions += [q for f, q in bq.items() if f not in have_q]
    answered = {_answer_field(a) for a in answers if str(a.get("answer") or "").strip()}
    questions = [dict(q, id=f"q{i + 1}") for i, q in enumerate(questions[:12])]
    have_r = {r.get("field") for r in risks if r.get("level") == "high"}
    risks += [r for r in baseline["risks"] if r["level"] == "high" and r["field"] not in have_r and r["field"] not in answered]
    risks = [r for r in risks if not (r.get("field") in answered and by.get(r["field"], {}).get("params", {}).get("policy") == "approved_default")]

    ordered = sorted(by.values(), key=lambda m: E.TARGET_ORDER.index(m["target"]))
    dr = E.dry_run(ordered, records)
    if "error" not in dr:  # report what the real dry run will quarantine, exactly like the baseline does
        incompat = [i for i in incompat if i["kind"] != "value_not_covered"]
        for e in dr["error_summary"][:8]:
            ex = next((r for r in dr["rejected"] if any(x["field"] == e["field"] and x["code"] == e["code"] for x in r["errors"])), None)
            err = next((x for x in ex["errors"] if x["field"] == e["field"] and x["code"] == e["code"]), None) if ex else None
            sv = f', for example {json.dumps(err["source_value"])}' if err and err.get("source_value") is not None else ""
            incompat.append({"kind": "value_not_covered", "field": e["field"], "text": f'{e["count"]} record(s) fail "{e["code"]}" on {e["field"]}{sv}. They will be quarantined.'})
    summary = norm["summary"] + (" Adjustments after validation: " + " ".join(notes) if notes else "")
    return {**norm, "summary": E.clip(summary, 800), "mappings": ordered, "risks": risks[:20], "questions": questions, "incompatibilities": incompat[:20]}


def run_agent(client, records: list[dict], settings: Settings, *, base: dict | None = None, answers: list[dict] | None = None, emit: Emit | None = None) -> dict:
    """Returns {"proposal": normalised dict, "model": str, "trace": [...]}. Raises AIServiceError subclasses on any failure."""
    emit = emit or (lambda *a, **k: None)
    box = ToolBox(records)
    baseline = E.heuristic_propose(records)
    answers = answers or []
    messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _user_prompt(base, answers, baseline)}]
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
                norm = harden(_validate(raw), baseline, records, answers, settings)
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
                    norm = harden(_validate(args), baseline, records, answers, settings)
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
