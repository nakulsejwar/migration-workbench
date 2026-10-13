"""HTTP API. Thin: validate input, resolve identity, call the workflow service, shape the response."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..agent.openrouter import AIConfigurationError, AIProviderError, AIServiceError
from ..agent.planner import AgentError, run_agent
from ..audit import audit, log_event, persist_log
from ..config import Settings
from ..errors import AppError, conflict, not_found
from ..schemas import (AnswerBody, ApproveBody, DryRunOut, PlanCreate, PlanOut, ProposeBody, ReconciliationOut, RejectBody, RetryBody,
                       RunCreate, RunOut, SourceReplace, ErrorEnvelope)
from ..services import engine as E
from ..services import workflow as W

router = APIRouter(prefix="/api", responses={409: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}})


def get_db(request: Request):
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def current_user(request: Request, x_demo_user: Optional[str] = Header(None, description="Demo identity. NOT authentication; see README.")) -> str:
    """Demo identity: the header must name one of DEMO_USERS. Anyone who can reach the API can claim any listed name."""
    if not x_demo_user:
        raise AppError(401, "identity_required", "Send the X-Demo-User header with one of the configured demo users to make changes.")
    if x_demo_user not in request.app.state.settings.demo_users:
        raise AppError(403, "identity_not_allowed", f'"{x_demo_user}" is not a configured demo user.', {"allowed": list(request.app.state.settings.demo_users)})
    return x_demo_user


# ------------------------------------------------------------------ meta / schema
@router.get("/config", tags=["meta"])
def config(s: Settings = Depends(get_settings)):
    """Non-secret capabilities for the UI. The API key is never returned."""
    return {"demo_users": list(s.demo_users), "llm_configured": s.llm_configured, "primary_model": s.primary_model, "fallback_models": list(s.fallback_models),
            "max_records": E.MAX_RECORDS, "test_runner_enabled": s.enable_test_runner,
            "identity_note": "Demo identity only (X-Demo-User header). Not authentication and not role-based access control."}


@router.get("/schema/source", tags=["schema"])
def source_schema():
    return E.SOURCE_SCHEMA


@router.get("/schema/target", tags=["schema"])
def target_schema():
    return E.TARGET_SCHEMA


@router.get("/rules", tags=["schema"])
def rules():
    """The only transformation rules the engine (and therefore the agent) can use."""
    return E.describe_rules()


@router.get("/source", tags=["source"])
def source_meta(db: Session = Depends(get_db)):
    return W.snapshot_dict(W.current_snapshot(db))


@router.get("/source/sample", tags=["source"])
def source_sample(limit: int = Query(200, ge=1, le=E.MAX_RECORDS), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    snap = W.current_snapshot(db)
    return {"snapshot": W.snapshot_dict(snap), "records": snap.records[offset:offset + limit]}


@router.get("/source/profile/{field}", tags=["source"])
def source_profile(field: str, db: Session = Depends(get_db)):
    snap = W.current_snapshot(db)
    p = E.profile_field(snap.records, field)
    if "error" in p:
        raise not_found(f"Source field {field}")
    if field in ("dob", "signup_date"):
        p["date_order_evidence"] = E.date_order_evidence(snap.records, field)
    return p


@router.put("/source", tags=["source"], status_code=200)
def replace_source(body: SourceReplace, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Replace the source dataset (<= 200 records). Earlier dry runs and approvals become stale automatically."""
    return W.replace_source(db, body.records, body.label, user)


# ------------------------------------------------------------------ plans
@router.get("/plans", response_model=list[PlanOut], tags=["plans"])
def list_plans(db: Session = Depends(get_db)):
    from sqlalchemy import select
    from ..models import PlanVersion
    return [W.plan_dict(db, p) for p in db.scalars(select(PlanVersion).order_by(PlanVersion.version))]


@router.get("/plans/{version}", response_model=PlanOut, tags=["plans"])
def get_plan(version: int, db: Session = Depends(get_db)):
    return W.plan_dict(db, W.get_plan(db, version))


@router.get("/plans/{a}/diff/{b}", tags=["plans"])
def diff_plans(a: int, b: int, db: Session = Depends(get_db)):
    pa, pb = W.get_plan(db, a), W.get_plan(db, b)
    return {"from": a, "to": b, "changes": E.diff_mappings(pa.mappings, pb.mappings)}


@router.post("/plans", response_model=PlanOut, status_code=201, tags=["plans"])
def create_plan(body: PlanCreate, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Create a NEW immutable version (optionally based on an earlier one). Existing versions are never modified."""
    p = W.create_user_version(db, body.base_version, [m.model_dump() for m in body.mappings], user, body.summary)
    return W.plan_dict(db, p)


@router.post("/plans/propose", response_model=PlanOut, status_code=201, tags=["plans", "agent"],
             responses={502: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope}, 504: {"model": ErrorEnvelope}})
def propose(body: ProposeBody, request: Request, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Create a proposal. `ai` runs the constrained OpenRouter agent; `baseline` is deterministic and needs no LLM."""
    s: Settings = request.app.state.settings
    snap = W.current_snapshot(db)
    records = snap.records
    base = base_dict = None
    answers: list[dict] = []
    if body.base_version:
        base = W.get_plan(db, body.base_version)
        base_dict = W.plan_dict(db, base)
        answers = [{"question": q["text"], "answer": q["answer"]} for q in base_dict["questions"] if q["answer"]]
    db.commit()  # release the SQLite write lock BEFORE any slow LLM call
    trace: list[dict] = []
    model = None
    if body.mode == "baseline":
        prop, origin = E.heuristic_propose(records), "baseline"
    else:
        origin = "agent"
        if not s.llm_configured:
            audit(db, "agent_failed", user, mode="ai", code="ai_not_configured")
            db.commit()
            raise AppError(503, "ai_not_configured", "OPENROUTER_API_KEY is not configured on the server.", {"offer_baseline": True})
        try:
            res = run_agent(request.app.state.llm_factory(s), records, s, base=base_dict, answers=answers, emit=lambda lvl, ev, **f: log_event(None, lvl, ev, **f))
            prop, model, trace = res["proposal"], res["model"], res["trace"]
        except AIServiceError as exc:
            code, status = "ai_provider_error", 502
            details: dict = {"offer_baseline": True}
            if isinstance(exc, AgentError):
                code, details = exc.code, {**(exc.details if isinstance(exc.details, dict) else {"errors": exc.details}), "offer_baseline": True}
            elif isinstance(exc, AIConfigurationError):
                code, status = "ai_not_configured", 503
            elif isinstance(exc, AIProviderError):
                status = 504 if exc.status_code == 408 else 502
                details.update({"provider_status": exc.status_code, "provider_message": str(exc.details)[:500]})
            log_event(None, "error", "agent.failed", code=code, status=status)
            audit(db, "agent_failed", user, mode="ai", code=code, provider_status=getattr(exc, "status_code", None))
            db.commit()
            raise AppError(status, code, str(exc), details)
    p = W.create_plan(db, mappings=prop["mappings"], actor=user, origin=origin, parent=body.base_version if body.mode == "ai" else None, summary=prop["summary"],
                      risks=prop["risks"], questions=prop["questions"], incompatibilities=prop["incompatibilities"], dropped=prop["dropped"], model_used=model)
    if base:
        W.carry_answers(db, base, p)
    for t in trace:
        persist_log(db, "info" if t["outcome"] == "ok" else "warning", "agent.trace", plan_version=p.version, **{k: v for k, v in t.items() if k != "round"}, round=t["round"])
    audit(db, "agent_proposal", user, plan_version=p.version, mode=body.mode, model=model, tool_calls=len(trace), rejected_tool_calls=len([t for t in trace if t["outcome"] != "ok"]), dropped=len(prop["dropped"]))
    db.commit()
    W.run_dry_run(db, p, user)
    return W.plan_dict(db, p)


@router.post("/plans/{version}/questions/{qid}/answer", response_model=PlanOut, tags=["plans"])
def answer(version: int, qid: str, body: AnswerBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    p = W.get_plan(db, version)
    W.answer_question(db, p, qid, body.answer, user)
    return W.plan_dict(db, p)


# ------------------------------------------------------------------ dry run and decisions
@router.post("/plans/{version}/dry-run", response_model=DryRunOut, tags=["dry-run"])
def post_dry_run(version: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Deterministic, LLM-free dry run. Writes only the report; never touches the target."""
    return W.run_dry_run(db, W.get_plan(db, version), user)


@router.get("/plans/{version}/dry-run", response_model=DryRunOut, tags=["dry-run"])
def get_dry_run(version: int, db: Session = Depends(get_db)):
    dr = W.latest_dry_run(db, W.get_plan(db, version))
    if not dr:
        raise not_found(f"A dry run for version {version}")
    return W.dry_run_dict(db, dr)


@router.get("/plans/{version}/approval-gate", tags=["approval"])
def approval_gate(version: int, db: Session = Depends(get_db)):
    """What the server will check when you try to approve this version."""
    p = W.get_plan(db, version)
    snap = W.current_snapshot(db)
    dr = W.latest_dry_run(db, p)
    open_q = len([q for q in W.plan_dict(db, p)["questions"] if not q["answer"]])
    gate = E.approval_gate(p.plan_hash, p.mappings, None if not dr else {"plan_hash": dr.plan_hash, "source_hash": dr.source_hash, "counts": dr.counts}, snap.hash, open_q)
    return {**gate, "approval": W.approval_state(db, p, snap)}


@router.post("/plans/{version}/approve", tags=["approval"])
def approve(version: int, body: ApproveBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    return W.approve(db, W.get_plan(db, version), user, body.comment, body.acknowledge_risks, body.acknowledge_open_questions)


@router.post("/plans/{version}/reject", tags=["approval"])
def reject(version: int, body: RejectBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    return W.reject(db, W.get_plan(db, version), user, body.comment)


@router.post("/plans/{version}/revoke", tags=["approval"])
def revoke(version: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    return W.revoke(db, W.get_plan(db, version), user)


# ------------------------------------------------------------------ runs
@router.post("/runs", response_model=RunOut, status_code=201, tags=["runs"])
def create_run(body: RunCreate, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Execute an APPROVED plan version. The server re-verifies approval, plan hash and source data; there is no way to run an unapproved mapping."""
    run = W.start_run(db, W.get_plan(db, body.plan_version), user, body.simulate_failure_after)
    return W.run_dict(db, run)


@router.get("/runs", response_model=list[RunOut], tags=["runs"])
def list_runs(db: Session = Depends(get_db)):
    from sqlalchemy import select
    from ..models import MigrationRun
    return [W.run_dict(db, r) for r in db.scalars(select(MigrationRun).order_by(MigrationRun.id.desc()))]


@router.get("/runs/{ref}", response_model=RunOut, tags=["runs"])
def get_run(ref: str, db: Session = Depends(get_db)):
    return W.run_dict(db, W.get_run(db, ref))


@router.post("/runs/{ref}/retry", response_model=RunOut, tags=["runs"])
def retry(ref: str, body: RetryBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Safe to call on failed or completed runs: existing rows are skipped, never inserted twice."""
    return W.run_dict(db, W.retry_run(db, W.get_run(db, ref), user, body.simulate_failure_after))


@router.post("/runs/{ref}/rollback", response_model=RunOut, tags=["runs"])
def rollback(ref: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Removes only the rows this run inserted. A second rollback is rejected with 409."""
    return W.run_dict(db, W.rollback_run(db, W.get_run(db, ref), user))


@router.get("/runs/{ref}/quarantine", tags=["runs"])
def run_quarantine(ref: str, db: Session = Depends(get_db)):
    from sqlalchemy import select
    from ..models import QuarantineRecord
    run = W.get_run(db, ref)
    return [{"row": q.row, "source_key": q.source_key, "errors": q.errors} for q in db.scalars(select(QuarantineRecord).where(QuarantineRecord.run_id == run.id).order_by(QuarantineRecord.row))]


@router.post("/runs/{ref}/reconciliation", response_model=ReconciliationOut, status_code=201, tags=["reconciliation"])
def reconcile(ref: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Computes and stores a reconciliation report from the persisted target rows (formulas documented in the README)."""
    return W.reconcile_run(db, W.get_run(db, ref), user)


@router.get("/runs/{ref}/reconciliation", response_model=ReconciliationOut, tags=["reconciliation"])
def latest_reconciliation(ref: str, db: Session = Depends(get_db)):
    from sqlalchemy import select
    from ..models import ReconciliationReport
    run = W.get_run(db, ref)
    r = db.scalars(select(ReconciliationReport).where(ReconciliationReport.run_id == run.id).order_by(ReconciliationReport.id.desc()).limit(1)).first()
    if not r:
        raise not_found(f"A reconciliation report for {ref}")
    return {"id": r.id, "run": ref, "created_at": r.created_at, **r.report}


@router.get("/target", tags=["target"])
def target(db: Session = Depends(get_db)):
    """Contents of the mock target table."""
    return [{"account_id": k, "run_ref": v["run_ref"], "record": v["record"]} for k, v in sorted(W.target_map(db).items())]


# ------------------------------------------------------------------ audit, logs, tests
@router.get("/audit", tags=["observability"])
def audit_list(limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    return W.audit_events(db, limit)


@router.get("/audit/download", tags=["observability"])
def audit_download(db: Session = Depends(get_db)):
    return PlainTextResponse(json.dumps(W.audit_events(db, 5000), indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="audit-events.json"'})


@router.get("/logs", tags=["observability"])
def logs(level: Optional[str] = Query(None, pattern="^(debug|info|warning|error)$"), limit: int = Query(300, ge=1, le=2000), db: Session = Depends(get_db)):
    return W.app_logs(db, level, limit)


@router.get("/logs/download", tags=["observability"])
def logs_download(db: Session = Depends(get_db)):
    body = "\n".join(json.dumps(r) for r in reversed(W.app_logs(db, None, 5000)))
    return PlainTextResponse(body, media_type="application/x-ndjson", headers={"Content-Disposition": 'attachment; filename="workbench-logs.jsonl"'})


@router.post("/tests/run", tags=["observability"])
def run_tests(request: Request, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Runs the pytest suite in a subprocess and returns its real output. Disabled when ENABLE_TEST_RUNNER=0."""
    s: Settings = request.app.state.settings
    if not s.enable_test_runner:
        raise AppError(403, "test_runner_disabled", "The in-app test runner is disabled on this deployment.")
    backend = Path(__file__).resolve().parents[2]
    env = {**os.environ, "OPENROUTER_API_KEY": "", "PYTHONPATH": str(backend)}
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests"], cwd=backend, env=env, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise AppError(504, "tests_timeout", "The test run exceeded 300 seconds.")
    lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
    out = {"passed": proc.returncode == 0, "exit_code": proc.returncode, "summary": lines[-1] if lines else (proc.stderr.strip().splitlines() or [""])[-1], "output": (proc.stdout + proc.stderr)[-6000:]}
    audit(db, "tests_run", user, passed=out["passed"], summary=out["summary"][:200])
    db.commit()
    return out
