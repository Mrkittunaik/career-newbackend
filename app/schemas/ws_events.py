from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field

# =====================================================================
# /ws/autoapply  (bot-token auth, bidirectional)
# =====================================================================

# ---------- Backend -> Bot ----------

class TaskAssignedEvent(BaseModel):
    type: Literal["task_assigned"] = "task_assigned"
    task_id: str
    site: str
    search_query: str


class ApplyJobItem(BaseModel):
    link: str
    job_id: str


class ApplyToEvent(BaseModel):
    type: Literal["apply_to"] = "apply_to"
    task_id: str
    jobs: List[ApplyJobItem]


class AnswerGeneratedEvent(BaseModel):
    type: Literal["answer_generated"] = "answer_generated"
    question_id: str
    answer_text: str


class DailyLimitReachedEvent(BaseModel):
    type: Literal["daily_limit_reached"] = "daily_limit_reached"


class SessionStateEvent(BaseModel):
    type: Literal["session_state"] = "session_state"
    session_id: str
    status: Literal["running", "stopped", "completed"]


class TokenInvalidEvent(BaseModel):
    type: Literal["token_invalid"] = "token_invalid"


class FileReadyEvent(BaseModel):
    """NOT in the original master message contract -- category 7 addition,
    response to file_requested. file_url is the stored ref/path for the
    matched document; None if no matching document was found."""
    type: Literal["file_ready"] = "file_ready"
    request_id: str
    file_url: Optional[str] = None
    found: bool = True


BackendToBotEvent = Union[
    TaskAssignedEvent,
    ApplyToEvent,
    AnswerGeneratedEvent,
    DailyLimitReachedEvent,
    SessionStateEvent,
    TokenInvalidEvent,
    FileReadyEvent,
]


# ---------- Bot -> Backend ----------

class JobFoundItem(BaseModel):
    title: str
    company: str
    link: str
    description: str


class JobsFoundEvent(BaseModel):
    type: Literal["jobs_found"] = "jobs_found"
    task_id: str
    jobs: List[JobFoundItem]


class QuestionAskedEvent(BaseModel):
    type: Literal["question_asked"] = "question_asked"
    session_id: str
    question_id: str
    question_text: str
    field_type: str


class FileRequestedEvent(BaseModel):
    """NOT in the original master message contract -- added in category 7 so
    the bot can ask for a resume/cover-letter/etc file for a form field.
    Mirrors question_asked's shape since it's the same kind of "bot needs
    something to fill a field" handshake."""
    type: Literal["file_requested"] = "file_requested"
    session_id: str
    request_id: str
    field_label: str  # e.g. "Resume", "Upload your CV", "Cover Letter"


class ApplicationSubmittedEvent(BaseModel):
    type: Literal["application_submitted"] = "application_submitted"
    task_id: str
    job_id: str


class ApplicationSkippedEvent(BaseModel):
    type: Literal["application_skipped"] = "application_skipped"
    task_id: str
    job_id: str
    reason: Optional[str] = None


class ApplicationFailedEvent(BaseModel):
    type: Literal["application_failed"] = "application_failed"
    task_id: str
    job_id: str
    reason: Optional[str] = None


class HrEmailFoundEvent(BaseModel):
    type: Literal["hr_email_found"] = "hr_email_found"
    session_id: str
    email: str
    company: str
    job_link: str


class StartSessionEvent(BaseModel):
    type: Literal["start_session"] = "start_session"
    site: str


class StopSessionEvent(BaseModel):
    type: Literal["stop_session"] = "stop_session"
    session_id: str


BotToBackendEvent = Union[
    JobsFoundEvent,
    QuestionAskedEvent,
    FileRequestedEvent,
    ApplicationSubmittedEvent,
    ApplicationSkippedEvent,
    ApplicationFailedEvent,
    HrEmailFoundEvent,
    StartSessionEvent,
    StopSessionEvent,
]


# =====================================================================
# /ws/dashboard  (user-JWT auth, push-only Backend -> Website)
# =====================================================================

class BotStatusEvent(BaseModel):
    type: Literal["bot_status"] = "bot_status"
    online: bool
    last_seen: Optional[str] = None


class JobProgressUpdateEvent(BaseModel):
    type: Literal["job_progress_update"] = "job_progress_update"
    job_application: dict  # serialized JobApplicationResponse


class HrContactAddedEvent(BaseModel):
    type: Literal["hr_contact_added"] = "hr_contact_added"
    hr_contact: dict  # serialized HRContactResponse


class DailyCounterUpdateEvent(BaseModel):
    type: Literal["daily_counter_update"] = "daily_counter_update"
    applied_today: int
    limit: int


class ApplicationReplyReceivedEvent(BaseModel):
    """NOT in the original master message contract -- added alongside gmail_service
    so the website can show a live toast/badge when an employer reply is detected."""
    type: Literal["application_reply_received"] = "application_reply_received"
    job_application_id: str
    reply_snippet: str


BackendToDashboardEvent = Union[
    BotStatusEvent,
    JobProgressUpdateEvent,
    HrContactAddedEvent,
    DailyCounterUpdateEvent,
    ApplicationReplyReceivedEvent,
]


# =====================================================================
# Envelope helpers
# =====================================================================

class WSEnvelope(BaseModel):
    """Generic wrapper if you want a consistent top-level shape (type + payload)
    instead of flat dicts. Optional -- flat event models above validate fine on
    their own via `parse_ws_message`."""
    type: str
    payload: dict = Field(default_factory=dict)


_AUTOAPPLY_EVENT_MAP = {
    # backend -> bot
    "task_assigned": TaskAssignedEvent,
    "apply_to": ApplyToEvent,
    "answer_generated": AnswerGeneratedEvent,
    "daily_limit_reached": DailyLimitReachedEvent,
    "session_state": SessionStateEvent,
    "token_invalid": TokenInvalidEvent,
    "file_ready": FileReadyEvent,
    # bot -> backend
    "jobs_found": JobsFoundEvent,
    "question_asked": QuestionAskedEvent,
    "file_requested": FileRequestedEvent,
    "application_submitted": ApplicationSubmittedEvent,
    "application_skipped": ApplicationSkippedEvent,
    "application_failed": ApplicationFailedEvent,
    "hr_email_found": HrEmailFoundEvent,
    "start_session": StartSessionEvent,
    "stop_session": StopSessionEvent,
}

_DASHBOARD_EVENT_MAP = {
    "bot_status": BotStatusEvent,
    "job_progress_update": JobProgressUpdateEvent,
    "hr_contact_added": HrContactAddedEvent,
    "daily_counter_update": DailyCounterUpdateEvent,
    "application_reply_received": ApplicationReplyReceivedEvent,
}


def parse_autoapply_message(raw: dict):
    """Validate+parse an incoming /ws/autoapply message dict into its typed event
    model, using the `type` field as discriminator. Raises KeyError/ValidationError
    on unknown type or bad shape."""
    event_type = raw.get("type")
    model = _AUTOAPPLY_EVENT_MAP.get(event_type)
    if model is None:
        raise ValueError(f"Unknown /ws/autoapply event type: {event_type!r}")
    return model.model_validate(raw)


def parse_dashboard_message(raw: dict):
    """Same as parse_autoapply_message but for /ws/dashboard (backend-authored,
    mainly useful for testing the push payloads before sending)."""
    event_type = raw.get("type")
    model = _DASHBOARD_EVENT_MAP.get(event_type)
    if model is None:
        raise ValueError(f"Unknown /ws/dashboard event type: {event_type!r}")
    return model.model_validate(raw)
