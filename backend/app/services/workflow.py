"""Business workflow. The server is the only authority for approval, execution, retry, rollback and reconciliation.

Transaction rules (see database.py): every transaction is BEGIN IMMEDIATE, so check-then-write sequences are serialised.
Each public function commits its own unit of work; on an expected failure it raises AppError (after rolling back).

Execution / recovery strategy
  * A run is created only from a VALID approval (re-verified here, never trusted from the client).
  * idempotency_key = sha256(plan_hash | dry_run_fingerprint | snapshot_hash) + ':' + generation, UNIQUE in the database.
    A second POST for the same approved plan+data is rejected (409) unless the earlier run was rolled back.
  * Each attempt is one transaction: all rows commit together, or none do (real errors).
    The controlled failure simulator commits the prefix on purpose, to model a target that dies mid-batch.
  * run.checkpoint = index of the next accepted row to process. Retry of a FAILED run resumes from the checkpoint;
    retry of a COMPLETED run re-verifies from row 0 and must insert nothing.
  * Inserts are guarded twice: a pre-check inside the exclusive transaction and the account_id PRIMARY KEY.
  * Existing key + different content = conflict: recorded, never overwritten.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import audit, log_event, timed
from ..errors import AppError, conflict, invalid, not_found
from ..models import (AuditEvent, AppLog, DryRun, MigrationRun, PlanDecision, PlanVersion, QuarantineRecord, QuestionAnswer,
                      ReconciliationReport, RollbackOp, RunAttempt, SourceSnapshot, TargetAccount, now_iso)
from . import engine as E

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_source.json"


# ------------------------------------------------------------------ source data
def init_data(db: Session) -> None:
    if db.scalar(select(func.count()).select_from(SourceSnapshot)):
        return
    records = json.loads(SAMPLE_PATH.read_text())
    db.add(SourceSnapshot(hash=E.source_hash(records), label="Built-in sample", records=records, record_count=len(records), created_by="system"))
    for s in E.build_seed_target(records):
        db.add(TargetAccount(account_id=s["account_id"], record=s["record"], content_hash=s["hash"], run_ref="seed", seq=0))
    audit(db, "workbench_initialized", "system", records=len(records))
    db.commit()


def current_snapshot(db: Session) -> SourceSnapshot:
    snap = db.scalars(select(SourceSnapshot).order_by(SourceSnapshot.id.desc()).limit(1)).first()
    if snap is None:
        raise AppError(503, "not_initialised", "The database has not been initialised")
    return snap


def snapshot_dict(s: SourceSnapshot) -> dict:
    return {"id": s.id, "hash": s.hash, "label": s.label, "record_count": s.record_count, "created_at": s.created_at, "created_by": s.created_by}


def replace_source(db: Session, records: list[dict], label: str, actor: str) -> dict:
    if not records or len(records) > E.MAX_RECORDS:
        raise invalid("bad_record_count", f"Provide between 1 and {E.MAX_RECORDS} records")
    before = [p.version for p in list(db.scalars(select(PlanVersion))) if approval_state(db, p)["status"] == "valid"]
    known = {f["name"] for f in E.SOURCE_SCHEMA["fields"]}
    unknown = sorted({k for r in records for k in r} - known)
    snap = SourceSnapshot(hash=E.source_hash(records), label=label[:120], records=records, record_count=len(records), created_by=actor)
    db.add(snap)
    db.flush()
    audit(db, "source_replaced", actor, records=len(records), source_hash=snap.hash, unknown_fields=unknown, approvals_invalidated=before)
    db.commit()
    return {**snapshot_dict(snap), "unknown_fields": unknown, "approvals_invalidated": before}


def target_map(db: Session) -> dict[str, dict]:
    return {r.account_id: {"record": r.record, "hash": r.content_hash, "run_ref": r.run_ref} for r in db.scalars(select(TargetAccount))}


# ------------------------------------------------------------------ plans
def get_plan(db: Session, version: int) -> PlanVersion:
    p = db.scalars(select(PlanVersion).where(PlanVersion.version == version)).first()
    if not p:
        raise not_found(f"Plan version {version}")
    return p


def extract_plan_policies(mappings: list[dict]) -> tuple[dict, str | None]:
    policies = {}
    phone_region = None
    for m in mappings:
        t = m.get("target")
        r = m.get("rule")
        p = m.get("params") or {}
        if t in ("tier", "marketing_opt_in") and r == "default_value":
            policies[t] = {
                "field": t,
                "value": p.get("value"),
                "policy": p.get("policy", "approved_default"),
                "rationale": p.get("rationale"),
                "approved_by": p.get("approved_by"),
                "approved_at": p.get("approved_at"),
                "semantic_consequences": p.get("semantic_consequences"),
            }
        elif t == "phone_e164" and r == "normalize_phone":
            cc, reg = E._resolve_phone_region(p)
            phone_region = reg or cc
    return policies, phone_region


def create_plan(db: Session, *, mappings: list[dict], actor: str, origin: str, parent: int | None = None, summary: str = "",
                risks: list | None = None, questions: list | None = None, incompatibilities: list | None = None,
                dropped: list | None = None, model_used: str | None = None) -> PlanVersion:
    canon = E.canon(mappings)
    by_target = {m["target"]: m for m in mappings}
    stored = [{**c, "confidence": by_target[c["target"]].get("confidence"), "rationale": by_target[c["target"]].get("rationale", "")} for c in canon]
    nxt = (db.scalar(select(func.max(PlanVersion.version))) or 0) + 1
    p = PlanVersion(version=nxt, parent_version=parent, origin=origin, created_by=actor, mappings=stored, plan_hash=E.plan_hash(stored),
                    summary=summary, risks=risks or [], questions=questions or [], incompatibilities=incompatibilities or [],
                    dropped=dropped or [], model_used=model_used)
    db.add(p)
    db.flush()
    audit(db, "plan_created", actor, plan_version=p.version, parent=parent, origin=origin, plan_hash=p.plan_hash, model=model_used)
    policies, phone_reg = extract_plan_policies(stored)
    if origin == "user" and (policies or phone_reg):
        audit(db, "business_policy_approved", actor, plan_version=p.version, policies=policies, phone_default_region=phone_reg)
    return p


def create_user_version(db: Session, base_version: int | None, mappings: list[dict], actor: str, summary: str = "") -> PlanVersion:
    base = get_plan(db, base_version) if base_version is not None else None
    if base and E.plan_hash(mappings) == base.plan_hash:
        raise conflict("no_changes", f"These mappings are identical to version {base.version}; nothing new to save")
    p = create_plan(db, mappings=mappings, actor=actor, origin="user", parent=base.version if base else None,
                    summary=summary or (f"Edited by {actor} from v{base.version}." if base else f"Created by {actor}."),
                    risks=base.risks if base else [], questions=base.questions if base else [],
                    incompatibilities=base.incompatibilities if base else [])
    if base:  # carry answers forward as annotations on the new version
        for a in db.scalars(select(QuestionAnswer).where(QuestionAnswer.plan_version_id == base.id)):
            db.add(QuestionAnswer(plan_version_id=p.id, qid=a.qid, answer=a.answer, answered_by=a.answered_by))
    db.commit()
    return p


def answer_question(db: Session, p: PlanVersion, qid: str, answer: str, actor: str) -> None:
    if qid not in {q["id"] for q in p.questions}:
        raise not_found(f"Question {qid}")
    row = db.scalars(select(QuestionAnswer).where(QuestionAnswer.plan_version_id == p.id, QuestionAnswer.qid == qid)).first()
    if not answer.strip():
        if row:
            db.delete(row)
    elif row:
        row.answer, row.answered_by, row.answered_at = answer.strip(), actor, now_iso()
    else:
        db.add(QuestionAnswer(plan_version_id=p.id, qid=qid, answer=answer.strip(), answered_by=actor))
    audit(db, "question_answered", actor, plan_version=p.version, question=qid, answered=bool(answer.strip()))
    db.commit()


def _answers(db: Session, p: PlanVersion) -> dict[str, str]:
    return {a.qid: a.answer for a in db.scalars(select(QuestionAnswer).where(QuestionAnswer.plan_version_id == p.id))}


def approval_state(db: Session, p: PlanVersion, snap: SourceSnapshot | None = None) -> dict:
    """Is there a decision for this version, and is it STILL valid? Recomputed from scratch on every call."""
    d = db.scalars(select(PlanDecision).where(PlanDecision.plan_version_id == p.id).order_by(PlanDecision.id.desc()).limit(1)).first()
    if not d:
        return {"status": "none", "reasons": []}
    base = {"decision_id": d.id, "decision": d.decision, "by": d.approver, "at": d.created_at, "comment": d.comment, "plan_hash": d.plan_hash,
            "dry_run_hash": d.dry_run_hash, "unanswered_questions": d.unanswered_questions}
    if d.decision == "rejected":
        return {**base, "status": "rejected", "reasons": []}
    if d.revoked_at:
        return {**base, "status": "revoked", "reasons": [], "revoked_by": d.revoked_by, "revoked_at": d.revoked_at}
    snap = snap or current_snapshot(db)
    reasons: list[str] = []
    if d.plan_hash != p.plan_hash:
        reasons.append("plan_changed")
    if d.snapshot_hash != snap.hash:
        reasons.append("source_changed")
    else:
        res = E.dry_run(p.mappings, snap.records)
        if "error" in res or res["hash"] != d.dry_run_hash:
            reasons.append("dry_run_changed")
    return {**base, "status": "stale" if reasons else "valid", "reasons": reasons}


def latest_dry_run(db: Session, p: PlanVersion) -> DryRun | None:
    return db.scalars(select(DryRun).where(DryRun.plan_version_id == p.id).order_by(DryRun.id.desc()).limit(1)).first()


def plan_dict(db: Session, p: PlanVersion) -> dict:
    ans = _answers(db, p)
    snap = current_snapshot(db)
    st = approval_state(db, p, snap)
    dr = latest_dry_run(db, p)
    status = {"valid": "approved", "stale": "approval_stale", "rejected": "rejected", "revoked": "approval_revoked"}.get(st["status"]) or ("proposed" if p.origin in ("agent", "baseline") else "draft")
    return {"version": p.version, "parent": p.parent_version, "origin": p.origin, "created_by": p.created_by, "created_at": p.created_at,
            "hash": p.plan_hash, "status": status, "approval": None if st["status"] == "none" else st, "mappings": p.mappings, "summary": p.summary,
            "risks": p.risks, "incompatibilities": p.incompatibilities, "dropped": p.dropped, "model_used": p.model_used,
            "questions": [{**q, "answer": ans.get(q["id"])} for q in p.questions], "validation": E.validate_plan(p.mappings),
            "dry_run": None if not dr else {"id": dr.id, "fingerprint": dr.fingerprint, "counts": dr.counts, "created_at": dr.created_at, "stale": dr.source_hash != snap.hash}}


# ------------------------------------------------------------------ dry run
def run_dry_run(db: Session, p: PlanVersion, actor: str) -> dict:
    snap = current_snapshot(db)
    with timed() as t:
        a = E.dry_run(p.mappings, snap.records)
        if "error" in a:
            raise invalid(a["error"], a["message"], {"issues": a.get("issues", [])})
        verified = E.dry_run(p.mappings, snap.records)["hash"] == a["hash"]
    if not verified:
        raise AppError(500, "nondeterministic_dry_run", "Two identical dry runs disagreed; refusing to store the result")
    prev = latest_dry_run(db, p)
    dr = DryRun(plan_version_id=p.id, snapshot_id=snap.id, plan_hash=a["plan_hash"], source_hash=a["source_hash"], fingerprint=a["hash"], counts=a["counts"],
                error_summary=a["error_summary"], accepted_preview=a["accepted"][:8], verified=True, created_by=actor)
    db.add(dr)
    db.flush()
    for r in a["rejected"]:
        db.add(QuarantineRecord(dry_run_id=dr.id, row=r["row"], source_key=r["source_key"], errors=r["errors"]))
    repeat = bool(prev and prev.plan_hash == dr.plan_hash and prev.source_hash == dr.source_hash)
    audit(db, "dry_run", actor, plan_version=p.version, fingerprint=dr.fingerprint, counts=dr.counts, repeat_of_same_inputs=repeat,
          identical_to_previous=repeat and prev.fingerprint == dr.fingerprint)
    log_event(db, "info", "dry_run.completed", plan_version=p.version, fingerprint=dr.fingerprint, counts=dr.counts, duration_ms=t["ms"], status="ok")
    db.commit()
    out = dry_run_dict(db, dr)
    out["repeat_of_same_inputs"], out["identical_to_previous"] = repeat, repeat and prev.fingerprint == dr.fingerprint
    return out


def dry_run_dict(db: Session, dr: DryRun, with_quarantine: bool = True) -> dict:
    snap = current_snapshot(db)
    stale = dr.source_hash != snap.hash
    p = db.get(PlanVersion, dr.plan_version_id)
    out = {"id": dr.id, "plan_version": p.version, "fingerprint": dr.fingerprint, "plan_hash": dr.plan_hash, "source_hash": dr.source_hash, "counts": dr.counts,
           "error_summary": dr.error_summary, "accepted_preview": dr.accepted_preview, "verified": dr.verified, "stale": stale, "created_at": dr.created_at,
           "created_by": dr.created_by, "target_preview": None}
    if with_quarantine:
        out["quarantine"] = [{"row": q.row, "source_key": q.source_key, "errors": q.errors}
                             for q in db.scalars(select(QuarantineRecord).where(QuarantineRecord.dry_run_id == dr.id).order_by(QuarantineRecord.row))]
    if not stale:
        res = E.dry_run(p.mappings, current_snapshot(db).records)
        if "error" not in res:
            out["target_preview"] = E.preview_against_target(res["accepted"], target_map(db))
    return out


# ------------------------------------------------------------------ approval
def _require_fresh_dry_run(db: Session, p: PlanVersion) -> tuple[DryRun, SourceSnapshot, dict]:
    snap = current_snapshot(db)
    dr = latest_dry_run(db, p)
    res = E.dry_run(p.mappings, snap.records)
    if "error" in res:
        raise invalid(res["error"], res["message"], {"issues": res.get("issues", [])})
    if not dr or dr.plan_hash != p.plan_hash or dr.source_hash != snap.hash or dr.fingerprint != res["hash"]:
        raise conflict("dry_run_required", "Run a dry run of this exact version against the current source data before deciding on it")
    return dr, snap, res


def approve(db: Session, p: PlanVersion, actor: str, comment: str, ack_risks: bool, ack_open: bool) -> dict:
    if approval_state(db, p)["status"] == "valid":
        raise conflict("already_approved", f"Version {p.version} already has a valid approval")
    dr, snap, res = _require_fresh_dry_run(db, p)
    open_q = len([q for q in p.questions if q["id"] not in _answers(db, p)])
    gate = E.approval_gate(p.plan_hash, p.mappings, {"plan_hash": dr.plan_hash, "source_hash": dr.source_hash, "counts": dr.counts}, snap.hash, open_q)
    if not gate["can_approve"]:
        raise conflict("approval_gate_failed", "The approval checks did not pass", gate["checks"])
    if not ack_risks:
        raise invalid("acknowledgement_required", "You must confirm that you reviewed the risks and dropped fields")
    if open_q and not ack_open:
        raise invalid("open_questions", f"{open_q} clarification question(s) are unanswered; answer them or acknowledge them explicitly")
    d = PlanDecision(plan_version_id=p.id, decision="approved", approver=actor, plan_hash=p.plan_hash, snapshot_hash=snap.hash, dry_run_id=dr.id,
                     dry_run_hash=dr.fingerprint, comment=comment[:500], unanswered_questions=open_q)
    db.add(d)
    policies, phone_reg = extract_plan_policies(p.mappings)
    audit(db, "plan_approved", actor, plan_version=p.version, plan_hash=p.plan_hash, source_hash=snap.hash, dry_run_hash=dr.fingerprint, unanswered_questions=open_q, comment=comment[:200], policies=policies, phone_default_region=phone_reg)
    if policies or phone_reg:
        audit(db, "business_policy_approved", actor, plan_version=p.version, policies=policies, phone_default_region=phone_reg, comment=comment[:200])
    db.commit()
    return approval_state(db, p)


def reject(db: Session, p: PlanVersion, actor: str, comment: str) -> dict:
    snap = current_snapshot(db)
    dr = latest_dry_run(db, p)
    db.add(PlanDecision(plan_version_id=p.id, decision="rejected", approver=actor, plan_hash=p.plan_hash, snapshot_hash=snap.hash,
                        dry_run_id=dr.id if dr else None, dry_run_hash=dr.fingerprint if dr else None, comment=comment[:500]))
    audit(db, "plan_rejected", actor, plan_version=p.version, plan_hash=p.plan_hash, comment=comment[:200])
    db.commit()
    return approval_state(db, p)


def revoke(db: Session, p: PlanVersion, actor: str) -> dict:
    d = db.scalars(select(PlanDecision).where(PlanDecision.plan_version_id == p.id, PlanDecision.decision == "approved",
                                              PlanDecision.revoked_at.is_(None)).order_by(PlanDecision.id.desc()).limit(1)).first()
    if not d:
        raise conflict("nothing_to_revoke", "This version has no active approval")
    d.revoked_at, d.revoked_by = now_iso(), actor
    audit(db, "approval_revoked", actor, plan_version=p.version, decision_id=d.id)
    db.commit()
    return approval_state(db, p)


# ------------------------------------------------------------------ execution
def _base_key(p: PlanVersion, fingerprint: str, snap_hash: str) -> str:
    return hashlib.sha256(f"{p.plan_hash}|{fingerprint}|{snap_hash}".encode()).hexdigest()[:40]


def start_run(db: Session, p: PlanVersion, actor: str, simulate_failure_after: int | None) -> MigrationRun:
    st = approval_state(db, p)
    if st["status"] != "valid":
        code = "approval_stale" if st["status"] == "stale" else "approval_required"
        raise conflict(code, f"Version {p.version} cannot be executed: approval is {st['status']}", {"status": st["status"], "reasons": st["reasons"]})
    snap = current_snapshot(db)
    res = E.dry_run(p.mappings, snap.records)
    if "error" in res or res["hash"] != st["dry_run_hash"]:
        raise conflict("approval_stale", "The dry run no longer matches what was approved", {"reasons": ["dry_run_changed"]})
    base = _base_key(p, res["hash"], snap.hash)
    prior = list(db.scalars(select(MigrationRun).where(MigrationRun.idempotency_key.like(base + ":%"))))
    live = [r for r in prior if r.status != "rolled_back"]
    if live:
        raise conflict("run_exists", f"{live[0].run_ref} already exists for this approved plan and data (status {live[0].status}); retry or roll it back instead", {"run": live[0].run_ref, "status": live[0].status})
    pre = {r.account_id: r.content_hash for r in db.scalars(select(TargetAccount))}
    run = MigrationRun(plan_version_id=p.id, plan_version=p.version, decision_id=st["decision_id"], dry_run_id=db.scalar(select(DryRun.id).where(DryRun.fingerprint == res["hash"], DryRun.plan_version_id == p.id).order_by(DryRun.id.desc())),
                       snapshot_id=snap.id, snapshot_hash=snap.hash, plan_hash=p.plan_hash, dry_fingerprint=res["hash"], idempotency_key=f"{base}:{len(prior)}", status="running",
                       pre_count=len(pre), pre_hashes=pre, counts=res["counts"], checkpoint=0, inserted_keys=[], conflicts=[], created_by=actor)
    db.add(run)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("run_exists", "Another request is already executing this approved plan")
    run.run_ref = f"run-{run.id}"
    for r in res["rejected"]:
        db.add(QuarantineRecord(run_id=run.id, row=r["row"], source_key=r["source_key"], errors=r["errors"]))
    audit(db, "execute_started", actor, plan_version=p.version, run_ref=run.run_ref, dry_run_hash=res["hash"], simulate_failure_after=simulate_failure_after)
    db.commit()
    return _apply(db, run, res["accepted"], 0, "execute", actor, simulate_failure_after)


def _apply(db: Session, run: MigrationRun, accepted: list[dict], start: int, kind: str, actor: str, simulate: int | None) -> MigrationRun:
    run_id, run_ref = run.id, run.run_ref
    n_attempt = (db.scalar(select(func.count()).select_from(RunAttempt).where(RunAttempt.run_id == run_id)) or 0) + 1
    att = RunAttempt(run_id=run_id, n=n_attempt, kind=kind, from_checkpoint=start, simulate_failure_after=simulate)
    db.add(att)
    inserted: list[str] = []
    own = present = 0
    conflicts: list[dict] = []
    error: dict | None = None
    idx = len(accepted)
    with timed() as t:
        try:
            n = 0
            for i in range(start, len(accepted)):
                a = accepted[i]
                ex = db.get(TargetAccount, a["key"])
                if ex is not None:
                    c = E.classify_existing(ex.content_hash, ex.run_ref, a["hash"], run_ref)
                    if c == "conflict":
                        conflicts.append({"row": a["row"], "key": a["key"], "existing_run": ex.run_ref, "diff_fields": [k for k in a["record"] if E.stable(ex.record.get(k)) != E.stable(a["record"].get(k))]})
                    elif c == "skipped_own":
                        own += 1
                    else:
                        present += 1
                    continue
                if simulate is not None and n >= simulate:
                    error = {"code": "target_unavailable", "message": f"Simulated target outage after {n} inserts"}
                    idx = i
                    break
                db.add(TargetAccount(account_id=a["key"], record=a["record"], content_hash=a["hash"], run_ref=run_ref, seq=0))
                inserted.append(a["key"])
                n += 1
            db.flush()
        except Exception as exc:  # real failure: nothing from this attempt is kept
            db.rollback()
            return _record_unexpected_failure(db, run_id, kind, start, simulate, exc, actor)
    run = db.get(MigrationRun, run_id)
    att.inserted, att.skipped_own, att.skipped_present, att.conflicts = len(inserted), own, present, len(conflicts)
    att.not_attempted = (len(accepted) - idx) if error else 0
    att.to_checkpoint, att.error, att.finished_at = idx, error, now_iso()
    att.status = "failed" if error else "completed"
    run.inserted_keys = list(run.inserted_keys) + inserted
    merged = {c["key"]: c for c in run.conflicts}
    merged.update({c["key"]: c for c in conflicts})
    run.conflicts = list(merged.values())
    run.checkpoint = idx
    run.status = "failed" if error else "completed"
    run.finished_at = now_iso()
    audit(db, "retry_attempt" if kind == "retry" else "execute_attempt", actor, plan_version=run.plan_version, run_ref=run_ref, attempt=n_attempt, inserted=len(inserted),
          skipped_own=own, skipped_present=present, conflicts=len(conflicts), not_attempted=att.not_attempted, checkpoint=idx, status=run.status, error_code=error["code"] if error else None)
    log_event(db, "warning" if error else "info", "run.attempt", run_ref=run_ref, attempt=n_attempt, kind=kind, status=run.status, inserted=len(inserted), duration_ms=t["ms"], error_code=error["code"] if error else None)
    db.commit()
    return run


def _record_unexpected_failure(db: Session, run_id: int, kind: str, start: int, simulate: int | None, exc: Exception, actor: str) -> MigrationRun:
    run = db.get(MigrationRun, run_id)
    n = (db.scalar(select(func.count()).select_from(RunAttempt).where(RunAttempt.run_id == run_id)) or 0) + 1
    err = {"code": "unexpected_error", "message": type(exc).__name__ + ": " + str(exc)[:200]}
    db.add(RunAttempt(run_id=run_id, n=n, kind=kind, from_checkpoint=start, to_checkpoint=start, simulate_failure_after=simulate, error=err, status="failed", finished_at=now_iso()))
    run.status, run.finished_at = "failed", now_iso()
    audit(db, "retry_attempt" if kind == "retry" else "execute_attempt", actor, plan_version=run.plan_version, run_ref=run.run_ref, attempt=n, inserted=0, status="failed", error_code="unexpected_error")
    log_event(db, "error", "run.unexpected_error", run_ref=run.run_ref, attempt=n, error_type=type(exc).__name__)
    db.commit()
    return run


def get_run(db: Session, ref: str) -> MigrationRun:
    r = db.scalars(select(MigrationRun).where(MigrationRun.run_ref == ref)).first()
    if not r:
        raise not_found(f"Run {ref}")
    return r


def _rederive(db: Session, run: MigrationRun) -> dict:
    """Re-run the deterministic dry run against the run's OWN source snapshot and check it matches what was approved."""
    p = db.get(PlanVersion, run.plan_version_id)
    snap = db.get(SourceSnapshot, run.snapshot_id)
    res = E.dry_run(p.mappings, snap.records)
    if "error" in res or res["hash"] != run.dry_fingerprint:
        raise conflict("cannot_rederive", "The run's plan or source snapshot no longer reproduces the approved dry run")
    return res


def _fresh(db: Session, run_id: int) -> MigrationRun:
    """Re-read a run from the database (bypassing the session's identity map). Always do this under the write lock
    before deciding anything, otherwise a concurrent request's changes (status, ledger, checkpoint) could be overwritten."""
    return db.get(MigrationRun, run_id, populate_existing=True)


def retry_run(db: Session, run: MigrationRun, actor: str, simulate: int | None) -> MigrationRun:
    run = _fresh(db, run.id)  # first statement of the transaction: takes the write lock
    prev = run.status
    if prev not in ("failed", "completed"):
        db.rollback()
        raise conflict("run_in_progress" if prev in ("running", "rolling_back") else "retry_not_allowed", f"{run.run_ref} is {prev}; it cannot be retried")
    res = _rederive(db, run)  # raises (and we roll back) if the plan/snapshot no longer reproduces the approved dry run
    resume = run.checkpoint if prev == "failed" else 0
    run.status = "running"  # claim: other requests now see 'running' and are refused
    audit(db, "retry_requested", actor, plan_version=run.plan_version, run_ref=run.run_ref, previous_status=prev, resume_from=resume)
    db.commit()
    return _apply(db, run, res["accepted"], resume, "retry", actor, simulate)


def rollback_run(db: Session, run: MigrationRun, actor: str) -> MigrationRun:
    run = _fresh(db, run.id)  # takes the write lock; two concurrent rollbacks are serialised here
    run_id, ref, expected, status = run.id, run.run_ref, len(run.inserted_keys), run.status
    if status not in ("completed", "failed"):
        db.rollback()
        audit(db, "rollback_rejected", actor, run_ref=ref, reason=status)
        db.add(RollbackOp(run_id=run_id, requested_by=actor, status="rejected", detail={"reason": status}))
        db.commit()
        raise conflict("already_rolled_back" if status == "rolled_back" else "run_in_progress", f"{ref} is {status}; it cannot be rolled back")
    run.status = "rolling_back"
    removed = db.execute(delete(TargetAccount).where(TargetAccount.run_ref == ref), execution_options={"synchronize_session": False}).rowcount
    if removed != expected:  # something other than this run's own ledger changed: refuse, undo everything
        db.rollback()
        audit(db, "rollback_rejected", actor, run_ref=ref, reason="ledger_mismatch", would_remove=removed, expected=expected)
        db.add(RollbackOp(run_id=run_id, requested_by=actor, status="rejected", detail={"reason": "ledger_mismatch", "would_remove": removed, "expected": expected}))
        log_event(db, "error", "rollback.unsafe", run_ref=ref, would_remove=removed, expected=expected)
        db.commit()
        raise conflict("rollback_unsafe", f"Rollback would remove {removed} rows but the run inserted {expected}; nothing was changed", {"would_remove": removed, "expected": expected})
    run = _fresh(db, run_id)
    run.status, run.finished_at = "rolled_back", now_iso()
    db.add(RollbackOp(run_id=run_id, requested_by=actor, status="completed", removed=removed))
    audit(db, "rollback", actor, plan_version=run.plan_version, run_ref=ref, removed=removed)
    log_event(db, "info", "rollback.completed", run_ref=ref, removed=removed, status="ok")
    db.commit()
    return run


# ------------------------------------------------------------------ reconciliation
def reconcile_run(db: Session, run: MigrationRun, actor: str) -> dict:
    res = _rederive(db, run)
    q = [{"row": x.row, "source_key": x.source_key, "errors": x.errors} for x in db.scalars(select(QuarantineRecord).where(QuarantineRecord.run_id == run.id).order_by(QuarantineRecord.row))]
    rb = db.scalars(select(RollbackOp).where(RollbackOp.run_id == run.id, RollbackOp.status == "completed")).first()
    rep = E.reconcile({"id": run.run_ref, "status": run.status, "inserted_keys": run.inserted_keys, "quarantine": q, "pre": run.pre_hashes, "pre_count": run.pre_count,
                       "rollback": {"removed": rb.removed} if rb else None}, res, target_map(db))
    row = ReconciliationReport(run_id=run.id, created_by=actor, passed=rep["passed"], report=rep)
    db.add(row)
    db.flush()
    audit(db, "reconciliation", actor, plan_version=run.plan_version, run_ref=run.run_ref, passed=rep["passed"], failed_checks=[c["id"] for c in rep["checks"] if not c["ok"]])
    log_event(db, "info" if rep["passed"] else "warning", "reconciliation.completed", run_ref=run.run_ref, passed=rep["passed"])
    db.commit()
    return {"id": row.id, "run": run.run_ref, "created_at": row.created_at, **rep}


def run_dict(db: Session, run: MigrationRun) -> dict:
    atts = list(db.scalars(select(RunAttempt).where(RunAttempt.run_id == run.id).order_by(RunAttempt.n)))
    rb = db.scalars(select(RollbackOp).where(RollbackOp.run_id == run.id).order_by(RollbackOp.id.desc())).all()
    return {"id": run.run_ref, "plan_version": run.plan_version, "status": run.status, "counts": run.counts, "pre_count": run.pre_count, "checkpoint": run.checkpoint,
            "inserted_total": len(run.inserted_keys), "conflicts": run.conflicts, "created_at": run.created_at, "created_by": run.created_by, "finished_at": run.finished_at,
            "dry_run_fingerprint": run.dry_fingerprint, "idempotency_key": run.idempotency_key,
            "attempts": [{"n": a.n, "kind": a.kind, "started_at": a.started_at, "finished_at": a.finished_at, "from_checkpoint": a.from_checkpoint, "to_checkpoint": a.to_checkpoint,
                          "inserted": a.inserted, "skipped_own": a.skipped_own, "skipped_present": a.skipped_present, "conflicts": a.conflicts, "not_attempted": a.not_attempted,
                          "simulate_failure_after": a.simulate_failure_after, "error": a.error, "status": a.status} for a in atts],
            "rollbacks": [{"status": r.status, "removed": r.removed, "by": r.requested_by, "at": r.created_at, "detail": r.detail} for r in rb]}


# ------------------------------------------------------------------ audit / logs
def audit_events(db: Session, limit: int = 200) -> list[dict]:
    return [{"id": e.id, "ts": e.ts, "type": e.type, "actor": e.actor, "request_id": e.request_id, "plan_version": e.plan_version, "run_ref": e.run_ref, "detail": e.detail}
            for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit))]


def app_logs(db: Session, level: str | None = None, limit: int = 300) -> list[dict]:
    q = select(AppLog).order_by(AppLog.id.desc()).limit(limit)
    if level:
        q = q.where(AppLog.level == level)
    return [{"id": l.id, "ts": l.ts, "level": l.level, "event": l.event, "request_id": l.request_id, "fields": l.fields} for l in db.scalars(q)]


def carry_answers(db: Session, base: PlanVersion, new: PlanVersion) -> None:
    """When a revision keeps a question with identical text, keep the reviewer's earlier answer as an annotation."""
    old = {q["id"]: q["text"] for q in base.questions}
    answers = {old[a.qid]: a for a in db.scalars(select(QuestionAnswer).where(QuestionAnswer.plan_version_id == base.id)) if a.qid in old}
    for q in new.questions:
        a = answers.get(q["text"])
        if a:
            db.add(QuestionAnswer(plan_version_id=new.id, qid=q["id"], answer=a.answer, answered_by=a.answered_by))
