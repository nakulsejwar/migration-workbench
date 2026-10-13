"""SQLAlchemy 2.x models. Plan versions and audit events are immutable at the DATABASE level (triggers)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DDL, JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SourceSnapshot(Base):
    """An immutable copy of the source dataset. The newest row is the current source."""
    __tablename__ = "source_snapshot"
    id: Mapped[int] = mapped_column(primary_key=True)
    hash: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(String(120))
    records: Mapped[list] = mapped_column(JSON)
    record_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso)
    created_by: Mapped[str] = mapped_column(String(64))


class PlanVersion(Base):
    __tablename__ = "plan_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    parent_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    origin: Mapped[str] = mapped_column(String(16))  # agent | baseline | user
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso)
    mappings: Mapped[list] = mapped_column(JSON)
    plan_hash: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    risks: Mapped[list] = mapped_column(JSON, default=list)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    incompatibilities: Mapped[list] = mapped_column(JSON, default=list)
    dropped: Mapped[list] = mapped_column(JSON, default=list)
    model_used: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)


class QuestionAnswer(Base):
    """Reviewer answers are annotations stored beside the immutable version, not inside it."""
    __tablename__ = "question_answer"
    __table_args__ = (UniqueConstraint("plan_version_id", "qid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("plan_version.id"), index=True)
    qid: Mapped[str] = mapped_column(String(16))
    answer: Mapped[str] = mapped_column(Text)
    answered_by: Mapped[str] = mapped_column(String(64))
    answered_at: Mapped[str] = mapped_column(String(32), default=now_iso)


class DryRun(Base):
    __tablename__ = "dry_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("plan_version.id"), index=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("source_snapshot.id"))
    plan_hash: Mapped[str] = mapped_column(String(32))
    source_hash: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(32))
    counts: Mapped[dict] = mapped_column(JSON)
    error_summary: Mapped[list] = mapped_column(JSON)
    accepted_preview: Mapped[list] = mapped_column(JSON)
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso)
    created_by: Mapped[str] = mapped_column(String(64))


class PlanDecision(Base):
    __tablename__ = "plan_decision"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("plan_version.id"), index=True)
    decision: Mapped[str] = mapped_column(String(16))  # approved | rejected
    approver: Mapped[str] = mapped_column(String(64))  # demo identity from the X-Demo-User header, never from the body
    plan_hash: Mapped[str] = mapped_column(String(32))
    snapshot_hash: Mapped[str] = mapped_column(String(32))
    dry_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("dry_run.id"), nullable=True)
    dry_run_hash: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    unanswered_questions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso)
    revoked_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    revoked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class MigrationRun(Base):
    __tablename__ = "migration_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_ref: Mapped[Optional[str]] = mapped_column(String(24), unique=True, nullable=True)
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("plan_version.id"), index=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    decision_id: Mapped[int] = mapped_column(ForeignKey("plan_decision.id"))
    dry_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("dry_run.id"), nullable=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("source_snapshot.id"))
    snapshot_hash: Mapped[str] = mapped_column(String(32))
    plan_hash: Mapped[str] = mapped_column(String(32))
    dry_fingerprint: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # running|completed|failed|rolling_back|rolled_back
    pre_count: Mapped[int] = mapped_column(Integer)
    pre_hashes: Mapped[dict] = mapped_column(JSON)
    counts: Mapped[dict] = mapped_column(JSON)
    checkpoint: Mapped[int] = mapped_column(Integer, default=0)
    inserted_keys: Mapped[list] = mapped_column(JSON, default=list)  # ledger of keys this run inserted
    conflicts: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso)
    created_by: Mapped[str] = mapped_column(String(64))
    finished_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class RunAttempt(Base):
    __tablename__ = "run_attempt"
    __table_args__ = (UniqueConstraint("run_id", "n"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("migration_run.id"), index=True)
    n: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # execute | retry
    started_at: Mapped[str] = mapped_column(String(32), default=now_iso)
    finished_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    from_checkpoint: Mapped[int] = mapped_column(Integer, default=0)
    to_checkpoint: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    skipped_own: Mapped[int] = mapped_column(Integer, default=0)
    skipped_present: Mapped[int] = mapped_column(Integer, default=0)
    conflicts: Mapped[int] = mapped_column(Integer, default=0)
    not_attempted: Mapped[int] = mapped_column(Integer, default=0)
    simulate_failure_after: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")


class QuarantineRecord(Base):
    """Rejected-row evidence. Belongs either to a dry run or to an execution run (copied at execution time)."""
    __tablename__ = "quarantine_record"
    __table_args__ = (CheckConstraint("(dry_run_id IS NOT NULL) + (run_id IS NOT NULL) = 1", name="ck_quarantine_owner"),
                      Index("ix_quarantine_dry", "dry_run_id"), Index("ix_quarantine_run", "run_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    dry_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("dry_run.id"), nullable=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("migration_run.id"), nullable=True)
    row: Mapped[int] = mapped_column(Integer)
    source_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    errors: Mapped[list] = mapped_column(JSON)


class TargetAccount(Base):
    """The mock target table. account_id PRIMARY KEY is the database-level duplicate guard."""
    __tablename__ = "target_account"
    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    record: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(32))
    run_ref: Mapped[str] = mapped_column(String(24), index=True)  # 'seed' for pre-existing rows
    seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso)


class ReconciliationReport(Base):
    __tablename__ = "reconciliation_report"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("migration_run.id"), index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso)
    created_by: Mapped[str] = mapped_column(String(64))
    passed: Mapped[bool] = mapped_column(Boolean)
    report: Mapped[dict] = mapped_column(JSON)


class RollbackOp(Base):
    __tablename__ = "rollback_op"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("migration_run.id"), index=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), default=now_iso)
    status: Mapped[str] = mapped_column(String(16))  # completed | rejected
    removed: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class AuditEvent(Base):
    """Append-only audit trail (UPDATE/DELETE blocked by triggers)."""
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), default=now_iso, index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    plan_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    run_ref: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class AppLog(Base):
    __tablename__ = "app_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), default=now_iso, index=True)
    level: Mapped[str] = mapped_column(String(8), index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    fields: Mapped[dict] = mapped_column(JSON, default=dict)


def _trigger(table: str, op: str, msg: str) -> DDL:
    return DDL(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, '{msg}'); END;")


for _tbl, _ops, _msg in ((PlanVersion.__table__, ("UPDATE", "DELETE"), "plan versions are immutable"),
                         (AuditEvent.__table__, ("UPDATE", "DELETE"), "audit events are append-only")):
    for _op in _ops:
        event.listen(_tbl, "after_create", _trigger(_tbl.name, _op, _msg).execute_if(dialect="sqlite"))
