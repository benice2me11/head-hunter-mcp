"""Persist the exact application presented to the user; never send while preparing."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import uuid

from hh_mcp_server.constants import DRAFT_DIR
from hh_mcp_server.private_files import write_private_json


def validate_ids(vacancy_id: str, resume_id: str) -> None:
    if not re.fullmatch(r"[0-9]{1,20}", vacancy_id):
        raise ValueError("Invalid vacancy ID")
    if not re.fullmatch(r"[a-f0-9]{20,100}", resume_id):
        raise ValueError("Invalid resume ID")


def content_digest(content: dict) -> str:
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def draft_path(draft_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", draft_id):
        raise ValueError("Invalid draft ID")
    return DRAFT_DIR / f"{draft_id}.json"


def create_draft(content: dict) -> dict:
    validate_ids(content["vacancy_id"], content["resume_id"])
    for name, answer in (content.get("question_answers") or {}).items():
        if not re.fullmatch(r"task_[A-Za-z0-9_]{1,120}", name) or not isinstance(answer, str):
            raise ValueError("Unsupported questionnaire field; review the form manually")
    draft = {
        "draft_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "prepared",
        "digest": content_digest(content),
        "content": content,
    }
    save_draft(draft)
    return draft


def save_draft(draft: dict) -> None:
    write_private_json(draft_path(draft["draft_id"]), draft)


def load_confirmed_draft(draft_id: str, approved_digest: str, confirmed: bool) -> dict:
    if confirmed is not True:
        raise ValueError("User confirmation of this exact draft is required before any application action.")
    draft = json.loads(draft_path(draft_id).read_text())
    if draft["status"] != "prepared":
        raise ValueError("Draft has already been dispatched or completed. Check history; do not resend.")
    if draft["digest"] != approved_digest or content_digest(draft["content"]) != approved_digest:
        raise ValueError("Application content changed after review; prepare and confirm a new draft.")
    if datetime.now(timezone.utc) - datetime.fromisoformat(draft["created_at"]) > timedelta(hours=24):
        raise ValueError("Draft expired. Recheck the vacancy and obtain confirmation for a fresh draft.")
    validate_ids(draft["content"]["vacancy_id"], draft["content"]["resume_id"])
    return draft
