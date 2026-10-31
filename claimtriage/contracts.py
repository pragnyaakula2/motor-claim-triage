"""Data contracts for every external interface (D1).

* FNOL v1            - inbound claim (POST /v1/claims)
* RunAccepted/Status - async run handle and polling response
* RunResult          - the recommendation returned to the caller
* Feedback           - caller feedback (L6)
* QuarantineRecord   - what a rejected submission looks like
* Retrieval contracts - search/clause API between worker and retrieval service

JSON Schemas are generated into ``contracts/`` by ``python -m claimtriage.contracts``.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "fnol.v1"

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]

REG_RE = re.compile(r"^([A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{4}|\d{2}\s?BH\s?\d{4}\s?[A-Z]{1,2})$")
POLICY_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-/ ]{7,29}$")
PHONE_RE = re.compile(r"^\+?\d{10,13}$")
PIN_RE = re.compile(r"^[1-9]\d{5}$")


class ClaimTypeDeclared(str, Enum):
    own_damage = "own_damage"
    third_party = "third_party"
    theft = "theft"
    total_loss = "total_loss"
    unknown = "unknown"


class VehicleClass(str, Enum):
    private_car = "private_car"
    two_wheeler = "two_wheeler"
    commercial = "commercial"
    other = "other"


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str = Field(min_length=2, max_length=60)
    state: str
    pincode: Optional[str] = None

    @field_validator("state")
    @classmethod
    def _state(cls, v: str) -> str:
        if v not in INDIAN_STATES:
            raise ValueError("state must be one of the Indian states/UTs listed in the data contract")
        return v

    @field_validator("pincode")
    @classmethod
    def _pin(cls, v):
        if v is not None and not PIN_RE.match(v):
            raise ValueError("pincode must be 6 digits and not start with 0")
        return v


class Vehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    registration: str
    make: str = Field(min_length=2, max_length=40)
    model: str = Field(min_length=1, max_length=60)
    year_of_manufacture: int = Field(ge=1990, le=date.today().year + 1)
    vehicle_class: VehicleClass
    idv_inr: float = Field(gt=0, le=50_000_000, description="Insured Declared Value in INR")
    drivable: bool

    @field_validator("registration")
    @classmethod
    def _reg(cls, v: str) -> str:
        v = v.strip().upper()
        if not REG_RE.match(v):
            raise ValueError("registration does not match an Indian registration format (e.g. MH 03 AB 1212)")
        return v


class Driver(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_owner: bool
    age: int = Field(ge=18, le=99)
    licence_valid: Optional[bool] = None


class PoliceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filed: bool
    fir_number: Optional[str] = Field(default=None, min_length=3, max_length=40)


class FNOL(BaseModel):
    """First Notice of Loss - the inbound request body."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"x-contract-version": CONTRACT_VERSION})

    client_reference: Optional[str] = Field(default=None, max_length=64)
    policy_number: str
    policy_start_date: date
    policy_end_date: date
    date_of_loss: date
    date_reported: Optional[date] = None
    location: Location
    vehicle: Vehicle
    driver: Driver
    claim_type_declared: ClaimTypeDeclared = ClaimTypeDeclared.unknown
    estimated_repair_cost_inr: Optional[float] = Field(default=None, ge=0, le=50_000_000)
    third_party_involved: bool = False
    third_party_damage_inr: float = Field(default=0, ge=0, le=50_000_000)
    injuries_reported: bool = False
    injured_count: int = Field(default=0, ge=0, le=20)
    police_report: PoliceReport = PoliceReport(filed=False)
    contact_phone: Optional[str] = None
    claim_description: str = Field(min_length=20, max_length=2000)
    claimed_damage: str = Field(min_length=3, max_length=500)

    @field_validator("policy_number")
    @classmethod
    def _pol(cls, v: str) -> str:
        v = v.strip().upper()
        if not POLICY_RE.match(v):
            raise ValueError("policy_number must be 8-30 chars of A-Z, 0-9, '-', '/' or space")
        return v

    @field_validator("contact_phone")
    @classmethod
    def _phone(cls, v):
        if v is not None and not PHONE_RE.match(v):
            raise ValueError("contact_phone must be 10-13 digits with optional leading +")
        return v

    @field_validator("claim_description", "claimed_damage")
    @classmethod
    def _text(cls, v: str) -> str:
        v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", v).strip()
        if len(v) < 3:
            raise ValueError("text is empty after removing control characters")
        return v


FREE_TEXT_FIELDS = ("claim_description", "claimed_damage")


# ---------------------------------------------------------------------------
# Outbound contracts
# ---------------------------------------------------------------------------
RunStatusLiteral = Literal["queued", "triaging", "analyzing", "completed", "escalated", "failed"]


class RunAccepted(BaseModel):
    run_id: str
    status: Literal["queued"] = "queued"
    status_url: str
    trace_url: str
    correlation_id: str
    cached: bool = False


class Citation(BaseModel):
    clause_id: str
    source: str
    page_start: int
    page_end: int
    section: str
    role: str
    quote: str


class Escalation(BaseModel):
    to_human: bool
    reason_code: Optional[str] = None
    detail: Optional[str] = None
    escalation_path: list[str] = []


class Recommendation(BaseModel):
    claim_type: str
    coverage_position: str
    summary: str
    route: Optional[Literal["fast_track", "surveyor_assignment", "investigation"]] = None
    route_reason: Optional[str] = None
    within_permitted_use: Literal["yes", "no", "unverifiable", "not_applicable"] = "not_applicable"
    citations: list[Citation] = []
    confidence: Literal["none", "low", "medium", "high"] = "none"
    advisory_notice: str = (
        "Advisory only. This is a recommendation with supporting evidence; it is not an approval or rejection. "
        "A human claims handler decides."
    )


class RunResult(BaseModel):
    run_id: str
    status: RunStatusLiteral
    degraded: bool = False
    recommendation: Optional[Recommendation] = None
    escalation: Optional[Escalation] = None
    verification: Optional[dict] = None
    security_flags: list[str] = []
    prompt_versions: dict = {}
    cost_usd: float = 0.0
    tokens: dict = {}
    timings: dict = {}
    partial_findings: Optional[dict] = None


class RunStatus(BaseModel):
    run_id: str
    status: RunStatusLiteral
    submitted_at: float
    updated_at: float
    result: Optional[RunResult] = None


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    satisfactory: bool
    reason: str = Field(min_length=3, max_length=1000)
    reviewer: Optional[str] = Field(default=None, max_length=64)


class FieldError(BaseModel):
    field: str
    code: str
    message: str


class QuarantineRecord(BaseModel):
    quarantine_id: str
    received_at: float
    client_id: str
    correlation_id: str
    reason: str
    errors: list[FieldError]
    payload_excerpt: dict | str


# retrieval service contracts ------------------------------------------------
class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=15)
    section_type: Optional[Literal["cover", "exclusion", "condition", "definition", "endorsement", "procedure", "admin"]] = None
    mode: Literal["hybrid", "dense", "keyword"] = "hybrid"


class Passage(BaseModel):
    clause_id: str
    source: str
    page_start: int
    page_end: int
    section: str
    section_type: str
    text: str
    score: float
    refs: list[str] = []


class SearchResponse(BaseModel):
    passages: list[Passage]
    index_version: str
    mode: str
    top_score: float


class ClauseRef(BaseModel):
    phrase: str
    resolved_to: list[str]
    status: Literal["resolved", "unresolved"]
    note: str = ""


class CrossRefResponse(BaseModel):
    clause_id: str
    refs: list[ClauseRef]
    suggested_queries: list[str]


def write_schemas(out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    models = {
        "fnol.v1": FNOL, "run_accepted": RunAccepted, "run_status": RunStatus, "run_result": RunResult,
        "feedback": Feedback, "quarantine_record": QuarantineRecord,
        "retrieval_search_request": SearchRequest, "retrieval_search_response": SearchResponse,
        "retrieval_cross_refs": CrossRefResponse,
    }
    written = []
    for name, model in models.items():
        p = out / f"{name}.schema.json"
        p.write_text(json.dumps(model.model_json_schema(), indent=2))
        written.append(p.name)
    return written


if __name__ == "__main__":
    print("wrote", write_schemas(Path(__file__).resolve().parent.parent / "contracts"))
