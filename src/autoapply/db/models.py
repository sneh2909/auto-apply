"""Pydantic document models for MongoDB.

These are plain Pydantic models — *not* ODM documents. Persistence is explicit:
fetch with `await get_db()[Job.collection].find_one(...)`, validate with `Job.model_validate(raw)`.
A few thin helpers below cover the common 80%.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Self

from bson import ObjectId
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from pymongo import ReturnDocument

from autoapply.db import get_db

# === Helpers ==============================================================


def _coerce_object_id(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, str):
        return v
    raise TypeError(f"cannot coerce {type(v)} to ObjectId string")


PyObjectId = Annotated[str | None, BeforeValidator(_coerce_object_id)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class MongoDoc(BaseModel):
    """Mixin for collection docs. Subclasses must set `collection: ClassVar[str]`."""

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        use_enum_values=True,
    )

    id: PyObjectId = Field(default=None, alias="_id")
    collection: ClassVar[str]

    def _to_doc(self) -> dict[str, Any]:
        d = self.model_dump(by_alias=True, exclude_none=True)
        d.pop("_id", None)
        return d

    async def insert(self) -> str:
        result = await get_db()[self.collection].insert_one(self._to_doc())
        self.id = str(result.inserted_id)
        return self.id

    async def upsert_by(self, filt: dict[str, Any]) -> str:
        result = await get_db()[self.collection].find_one_and_update(
            filt,
            {"$setOnInsert": self._to_doc()},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        self.id = str(result["_id"])
        return self.id

    async def save(self) -> None:
        if self.id:
            await get_db()[self.collection].replace_one(
                {"_id": ObjectId(self.id)}, self._to_doc()
            )
        else:
            await self.insert()

    @classmethod
    async def get(cls, doc_id: str) -> Self | None:
        raw = await get_db()[cls.collection].find_one({"_id": ObjectId(doc_id)})
        return cls.model_validate(raw) if raw else None

    @classmethod
    async def find_one(cls, filt: dict[str, Any]) -> Self | None:
        raw = await get_db()[cls.collection].find_one(filt)
        return cls.model_validate(raw) if raw else None

    @classmethod
    async def find_many(
        cls,
        filt: dict[str, Any] | None = None,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
    ) -> list[Self]:
        cursor = get_db()[cls.collection].find(filt or {})
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(limit)
        out: list[Self] = []
        async for raw in cursor:
            out.append(cls.model_validate(raw))
        return out


# === Enums ================================================================


class Channel(StrEnum):
    ats = "ats"           # A — Playwright auto-fill on a known ATS
    email_reply = "email" # B — reply to address found in alert email
    coldmail = "coldmail" # C — cold HR email via resolver cascade
    agent = "agent"       # D — browser-use natural-language agent


class AppStatus(StrEnum):
    queued = "queued"
    drafted = "drafted"
    approved = "approved"
    sent = "sent"
    submitted = "submitted"
    skipped = "skipped"
    failed = "failed"


class ReplyClass(StrEnum):
    interest = "interest"
    reject = "reject"
    auto_ack = "auto_ack"
    other = "other"


class AgentStatus(StrEnum):
    running = "running"
    awaiting_user = "awaiting_user"
    finished = "finished"
    cancelled = "cancelled"
    failed = "failed"


# === Documents ============================================================


class Job(MongoDoc):
    collection: ClassVar[str] = "jobs"

    source: str                                    # "gmail:naukri", "ats:greenhouse", ...
    source_id: str
    company: str
    role: str
    jd_text: str
    jd_url: str | None = None
    jd_url_key: str | None = None
    location: str | None = None
    remote: bool = False
    posted_at: datetime | None = None
    recruiter_name: str | None = None
    hr_email: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    dedup_hash: str

    fit_score: float | None = None
    fit_reasons: list[str] | None = None
    deal_breakers: list[str] | None = None

    ingested_at: datetime = Field(default_factory=utc_now)


class Application(MongoDoc):
    collection: ClassVar[str] = "applications"

    job_id: str
    channel: Channel
    status: AppStatus = AppStatus.queued

    draft_subject: str | None = None
    draft_body: str | None = None
    recipient: str | None = None
    ats_url: str | None = None
    agent_run_id: str | None = None

    sender_account: str = ""
    sent_at: datetime | None = None
    error: str | None = None
    # RFC822 Message-ID header of the sent email - used to match replies back via In-Reply-To
    sent_message_id: str | None = None

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Reply(MongoDoc):
    collection: ClassVar[str] = "replies"

    application_id: str
    gmail_message_id: str
    snippet: str
    classification: ReplyClass | None = None
    received_at: datetime = Field(default_factory=utc_now)


class AgentRun(MongoDoc):
    collection: ClassVar[str] = "agent_runs"

    goal: str
    parsed_intent: dict[str, Any] | None = None
    status: AgentStatus = AgentStatus.running
    steps_taken: int = 0
    jobs_discovered: list[str] = Field(default_factory=list)
    jobs_applied: list[str] = Field(default_factory=list)
    last_error: str | None = None
    transcript_path: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class SourceRun(MongoDoc):
    collection: ClassVar[str] = "source_runs"

    source: str
    started_at: datetime
    finished_at: datetime | None = None
    emails_seen: int = 0
    jobs_emitted: int = 0
    parse_errors: int = 0
    notes: str = ""
