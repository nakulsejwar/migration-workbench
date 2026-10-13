"""Pydantic request/response models. Rule and field names are validated against the supported catalogue."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .services import engine as E


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target: str
    source: Optional[str] = None
    rule: str
    params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    rationale: str = Field("", max_length=600)

    @field_validator("target")
    @classmethod
    def _target(cls, v: str) -> str:
        if v not in E.TF:
            raise ValueError(f'unknown target field "{v}"')
        return v

    @field_validator("rule")
    @classmethod
    def _rule(cls, v: str) -> str:
        if v not in E.RULES:
            raise ValueError(f'"{v}" is not a supported transformation rule')
        return v


class PlanCreate(BaseModel):
    base_version: Optional[int] = Field(None, ge=1)
    mappings: list[MappingIn] = Field(min_length=1, max_length=len(E.TARGET_SCHEMA["fields"]))
    summary: str = Field("", max_length=800)


class ProposeBody(BaseModel):
    mode: Literal["ai", "baseline"] = "baseline"
    base_version: Optional[int] = Field(None, ge=1, description="Revise this version using the reviewer's answers (ai mode)")


class AnswerBody(BaseModel):
    answer: str = Field(max_length=500)


class ApproveBody(BaseModel):
    comment: str = Field("", max_length=500)
    acknowledge_risks: bool = False
    acknowledge_open_questions: bool = False


class RejectBody(BaseModel):
    comment: str = Field(min_length=1, max_length=500)


class RunCreate(BaseModel):
    plan_version: int = Field(ge=1)
    simulate_failure_after: Optional[int] = Field(None, ge=0, le=E.MAX_RECORDS, description="Testing aid: stop after N inserts to model a target outage")


class RetryBody(BaseModel):
    simulate_failure_after: Optional[int] = Field(None, ge=0, le=E.MAX_RECORDS)


class SourceReplace(BaseModel):
    records: list[dict[str, Any]] = Field(min_length=1, max_length=E.MAX_RECORDS)
    label: str = Field("Imported by reviewer", max_length=120)


# ---- AI output contract (validated before anything is stored) -------------------------------------------------------
class RiskIn(BaseModel):
    level: Literal["high", "medium", "low"] = "medium"
    field: Optional[str] = None
    text: str = Field(max_length=600)


class QuestionIn(BaseModel):
    field: Optional[str] = None
    text: str = Field(max_length=600)
    assumption: Optional[str] = None


class IncompatIn(BaseModel):
    kind: str = "note"
    field: Optional[str] = None
    text: str = Field(max_length=600)


class UncertainIn(BaseModel):
    field: str
    reason: str = Field(max_length=400)


class AgentProposal(BaseModel):
    """Contract for the agent's final answer (submit_plan tool arguments). Unsupported rules are NOT rejected here:
    they are removed by engine.normalize_proposal with a recorded reason, so the reviewer sees what the model attempted."""
    model_config = ConfigDict(extra="ignore")
    summary: str = Field("", max_length=1200)
    mappings: list[dict[str, Any]] = Field(min_length=1)
    incompatibilities: list[IncompatIn] = Field(default_factory=list)
    risks: list[RiskIn] = Field(default_factory=list)
    questions: list[QuestionIn] = Field(default_factory=list)
    uncertain: list[UncertainIn] = Field(default_factory=list, description="Fields whose classification you are unsure about")


# ---- responses (permissive on purpose: they document the API, the service layer builds them) ------------------------
class Error(BaseModel):
    code: str
    message: str
    details: Any = None
    request_id: Optional[str] = None


class ErrorEnvelope(BaseModel):
    error: Error


class PlanOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: int
    parent: Optional[int]
    origin: str
    created_by: str
    created_at: str
    hash: str
    status: str
    mappings: list[dict[str, Any]]
    questions: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    incompatibilities: list[dict[str, Any]]
    validation: dict[str, Any]


class DryRunOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    plan_version: int
    fingerprint: str
    counts: dict[str, int]
    stale: bool


class RunOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    plan_version: int
    status: str
    attempts: list[dict[str, Any]]


class ReconciliationOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    run: str
    passed: bool
    checks: list[dict[str, Any]]
    totals: dict[str, Any]
