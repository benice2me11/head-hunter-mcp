"""Durable, process-safe journal for resume update attempts."""
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import uuid

from hh_mcp_server.constants import PROFILE_DIR
from hh_mcp_server.private_files import private_directory


KINDS = {
    "draft_prepared",
    "attempt_started",
    "suboperation_started",
    "dispatch_started",
    "suboperation_result",
    "attempt_result",
    "reconciliation",
}


def resume_update_context(draft: dict) -> dict:
    return {
        "draft_id": draft["draft_id"],
        "digest": draft["digest"],
        "resume_id": draft["content"]["resume_id"],
    }


class ResumeUpdateJournal(AbstractContextManager):
    def __init__(self, path=None):
        self.path = Path(path) if path else PROFILE_DIR / "resume_updates.jsonl"
        self.lock_fd = None
        self.handle = None
        self.events: list[dict] = []

    def __enter__(self):
        private_directory(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            self.lock_fd = os.open(self.path.with_suffix(".lock"), flags, 0o600)
            os.fchmod(self.lock_fd, 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Resume update journal is in use; do not bypass its lock"
                ) from None
            fd = os.open(self.path, flags, 0o600)
            os.fchmod(fd, 0o600)
            self.handle = os.fdopen(fd, "r+", encoding="utf-8")
            seen = set()
            for line in self.handle:
                if not line.endswith("\n"):
                    raise ValueError(
                        "Incomplete resume update journal record; reconcile before continuing"
                    )
                event = json.loads(line)
                if (
                    not isinstance(event, dict)
                    or event.get("schema_version") != 1
                    or event.get("kind") not in KINDS
                    or not event.get("event_id")
                    or event["event_id"] in seen
                    or not event.get("draft_id")
                    or not event.get("resume_id")
                ):
                    raise ValueError(
                        "Invalid resume update journal; reconcile before continuing"
                    )
                seen.add(event["event_id"])
                self.events.append(event)
            self.handle.seek(0, os.SEEK_END)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def append(
        self,
        kind: str,
        context: dict,
        *,
        attempt_id: str | None = None,
        operation_id: str | None = None,
        data: dict | None = None,
        event_id: str | None = None,
    ) -> dict:
        if self.handle is None or kind not in KINDS:
            raise ValueError("Use a locked resume update journal and a known event kind")
        event = {
            "schema_version": 1,
            "event_id": event_id or uuid.uuid4().hex,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **context,
            "attempt_id": attempt_id,
            "operation_id": operation_id,
            "data": data or {},
        }
        existing = next(
            (item for item in self.events if item["event_id"] == event["event_id"]), None
        )
        if existing is not None:
            comparable = lambda value: {
                key: item for key, item in value.items() if key != "occurred_at"
            }
            if comparable(existing) != comparable(event):
                raise ValueError("Event ID already exists with different content")
            return existing
        self.handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.events.append(event)
        return event

    def __exit__(self, *args):
        try:
            if self.handle is not None:
                self.handle.close()
        finally:
            self.handle = None
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
