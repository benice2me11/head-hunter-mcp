"""Fail-closed network gates for reviewed resume updates."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import asyncio
import json
import re
from urllib.parse import parse_qs, urlparse


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_READ_DOMAINS = ("hh.ru", "hhcdn.ru")
_VOLATILE_PARTIAL_KEYS = frozenset({"fingerprintIteration2", "fingerprintSp"})
_BULLETS = re.compile(r"[•▪‣⁃◦⦿◘◙○●■□▫]")
_EXPERIENCE_PROFILE_FIELDS = frozenset(
    {
        "id",
        "position",
        "professionId",
        "professionName",
        "description",
        "startDate",
        "endDate",
        "companyName",
        "companyUrl",
        "companyIndustries",
        "companyIndustryId",
        "companyAreaId",
        "companyId",
        "industries",
        "employerId",
        "companyState",
        "verification",
    }
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate resume update JSON key")
        result[key] = value
    return result


def _parse_json(request) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/json":
        raise ValueError("Resume update request must be JSON")
    value = json.loads(
        request.post_data_buffer or b"{}", object_pairs_hook=_unique_object
    )
    if not isinstance(value, dict):
        raise ValueError("Resume update JSON payload must be an object")
    return value


def _trusted_read(url: str) -> bool:
    target = urlparse(url)
    hostname = target.hostname or ""
    return target.scheme == "https" and any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in _READ_DOMAINS
    )


def _one_string(value, field: str) -> str:
    rows = value.get(field) or []
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError(f"Unsupported {field} shape")
    if not rows:
        return ""
    row = rows[0]
    if not isinstance(row, dict) or set(row) != {"string"}:
        raise ValueError(f"Unsupported {field} shape")
    return row["string"]


def _string_list(value: dict, field: str) -> list[str]:
    result = []
    for row in value.get(field) or []:
        if (
            not isinstance(row, dict)
            or set(row) != {"string"}
            or not isinstance(row["string"], str)
        ):
            raise ValueError(f"Unsupported {field} shape")
        result.append(row["string"])
    return result


def _position_payload(material: dict, title: str) -> dict:
    roles = []
    for row in material.get("professionalRole") or []:
        if not isinstance(row, dict):
            raise ValueError("Unsupported professionalRole shape")
        value = row.get("string", row.get("id"))
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("Unsupported professionalRole value") from None
        roles.append(value)
    return {
        "title": [title],
        "salary": deepcopy(material.get("salary") or []),
        "professionalRole": roles,
        "travelTime": _string_list(material, "travelTime"),
        "businessTripReadiness": _string_list(material, "businessTripReadiness"),
        "workFormats": _string_list(material, "workFormats"),
        "employmentForms": _string_list(material, "employmentForms"),
    }


def _experience_payload(snapshot: dict, operation: dict) -> dict:
    by_id: dict[int, dict] = {}
    resume_ids: dict[int, set[str]] = {}
    for resume_id, state in snapshot["resumes"].items():
        for row in state["material"].get("experience") or []:
            entry_id = row.get("id")
            if not isinstance(entry_id, int):
                raise ValueError("Experience entry is missing a stable ID")
            current = deepcopy(row)
            previous = by_id.get(entry_id)
            if previous is None:
                by_id[entry_id] = current
            else:
                left = {k: v for k, v in previous.items() if k != "description"}
                right = {k: v for k, v in current.items() if k != "description"}
                if left != right or previous.get("description") != current.get("description"):
                    raise ValueError("Shared experience state is inconsistent")
            resume_ids.setdefault(entry_id, set()).add(resume_id)

    target_id = operation["entry_id"]
    if target_id not in by_id:
        raise ValueError("Approved experience entry disappeared")

    prepared = []
    for entry_id in sorted(by_id):
        row = {
            key: deepcopy(value)
            for key, value in by_id[entry_id].items()
            if key in _EXPERIENCE_PROFILE_FIELDS
        }
        description = row.get("description") or ""
        if entry_id == target_id:
            description = operation["after"]
        if _BULLETS.search(description):
            raise ValueError(
                "HH would normalize bullet characters during experience save; edit manually"
            )
        row["description"] = description
        row["resumes"] = [
            {"hash": resume_id} for resume_id in sorted(resume_ids[entry_id])
        ]
        prepared.append(row)
    return {
        "profile": {"experience": prepared},
        "resumeFromHash": operation["affected_resume_ids"][0]
        if operation["affected_resume_ids"]
        else None,
    }


def _canonical_experience_payload(value: dict) -> dict:
    result = deepcopy(value)
    profile = result.get("profile")
    if not isinstance(profile, dict) or set(profile) != {"experience"}:
        raise ValueError("Unexpected profile update fields")
    rows = profile["experience"]
    if not isinstance(rows, list):
        raise ValueError("Experience profile payload must be a list")
    seen = set()
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            raise ValueError("Experience profile row is missing a stable ID")
        if row["id"] in seen:
            raise ValueError("Duplicate experience profile row")
        seen.add(row["id"])
        item = deepcopy(row)
        resumes = item.get("resumes")
        if not isinstance(resumes, list):
            raise ValueError("Experience profile row is missing resume associations")
        hashes = []
        for resume in resumes:
            if not isinstance(resume, dict) or set(resume) != {"hash"}:
                raise ValueError("Unsupported experience resume association")
            hashes.append(resume["hash"])
        if len(hashes) != len(set(hashes)):
            raise ValueError("Duplicate experience resume association")
        item["resumes"] = [{"hash": value} for value in sorted(hashes)]
        normalized.append(item)
    profile["experience"] = sorted(normalized, key=lambda item: item["id"])
    return result


@dataclass(frozen=True)
class ExpectedMutation:
    method: str
    host: str
    path: str
    payload: dict
    resume_id: str | None = None
    partial_resume_edit: bool = False
    experience_profile: bool = False


def build_expected_mutation(
    resume_id: str, operation: dict, base_snapshot: dict
) -> ExpectedMutation:
    kind = operation["kind"]
    if kind == "headline":
        material = base_snapshot["resumes"][resume_id]["material"]
        payload = _position_payload(material, operation["after"])
        return ExpectedMutation(
            "POST", "hh.ru", "/applicant/resume/edit", payload,
            resume_id=resume_id, partial_resume_edit=True,
        )
    if kind == "about":
        payload = {
            "skills": [operation["after"]] if operation["after"] else []
        }
        return ExpectedMutation(
            "POST", "hh.ru", "/applicant/resume/edit", payload,
            resume_id=resume_id, partial_resume_edit=True,
        )
    if kind == "skills":
        payload = {"keySkills": list(operation["after"])}
        return ExpectedMutation(
            "POST", "hh.ru", "/applicant/resume/edit", payload,
            resume_id=resume_id, partial_resume_edit=True,
        )
    if kind == "experience_description":
        payload = _experience_payload(base_snapshot, operation)
        payload["resumeFromHash"] = resume_id
        return ExpectedMutation(
            "POST", "resume-profile-front.hh.ru", "/profile/shards/profile/update", payload,
            experience_profile=True,
        )
    if kind == "skill_level":
        payload = {
            "userSkillLevels": [{
                "category": "SKILL",
                "level_id": operation["after_level_id"],
                "skill_id": operation["skill_id"],
            }]
        }
        return ExpectedMutation(
            "PUT", "hh.ru", "/shards/applicant/key_skills/levels/update", payload
        )
    raise ValueError(f"Unsupported resume update operation: {kind}")


class ReadOnlyGuard:
    """Permit trusted reads and physically abort every network mutation."""

    def __init__(self):
        self.blocked_mutations: list[tuple[str, str]] = []

    async def handle(self, route):
        request = route.request
        target = urlparse(request.url)
        if request.method in _READ_METHODS and _trusted_read(request.url):
            await route.fallback()
            return
        if request.method not in _READ_METHODS:
            self.blocked_mutations.append((request.method, target.path))
        await route.abort("blockedbyclient")


class ResumeUpdateGuard:
    """Permit exactly one approved HH mutation with an exact semantic payload."""

    def __init__(self, expected: ExpectedMutation, on_dispatch):
        self.expected = expected
        self.on_dispatch = on_dispatch
        self.dispatched = False
        self.request = None
        self.response_status = None
        self.error: str | None = None
        self.dispatch_event = asyncio.Event()
        self.response_event = asyncio.Event()

    def _validate_target(self, request) -> None:
        target = urlparse(request.url)
        if (
            target.scheme != "https"
            or (target.hostname or "") != self.expected.host
            or request.method != self.expected.method
            or target.path.rstrip("/") != self.expected.path
        ):
            raise ValueError("Unexpected resume mutation destination")
        query = parse_qs(target.query, keep_blank_values=True)
        if self.expected.partial_resume_edit:
            if query.get("resume") != [self.expected.resume_id]:
                raise ValueError("Resume edit request targets a different resume")
            if set(query) - {"resume", "hhtmSource"}:
                raise ValueError("Unexpected resume edit query parameter")
            if "hhtmSource" in query and len(query["hhtmSource"]) != 1:
                raise ValueError("Ambiguous hhtmSource")
        elif query:
            raise ValueError("Unexpected query parameters on resume mutation")

    def _validate_payload(self, request) -> None:
        actual = _parse_json(request)
        expected = deepcopy(self.expected.payload)
        if self.expected.partial_resume_edit:
            volatile = set(actual) & _VOLATILE_PARTIAL_KEYS
            for key in volatile:
                if not isinstance(actual[key], str):
                    raise ValueError("Invalid resume fingerprint metadata")
                actual.pop(key)
            if set(actual) != set(expected):
                raise ValueError("Unexpected partial resume edit fields")
        if self.expected.experience_profile:
            actual = _canonical_experience_payload(actual)
            expected = _canonical_experience_payload(expected)
        if actual != expected:
            raise ValueError("Resume mutation payload differs from approved content")

    async def handle(self, route):
        request = route.request
        if request.method in _READ_METHODS:
            if _trusted_read(request.url):
                await route.fallback()
            else:
                await route.abort("blockedbyclient")
            return
        if self.dispatched:
            self.error = "Duplicate resume mutation was blocked"
            await route.abort("blockedbyclient")
            return
        try:
            self._validate_target(request)
            self._validate_payload(request)
            self.on_dispatch()
        except BaseException as error:
            self.error = str(error) or type(error).__name__
            await route.abort("blockedbyclient")
            return
        self.dispatched = True
        self.request = request
        self.dispatch_event.set()
        await route.fallback()

    def observe_response(self, response):
        if self.request is not None and response.request == self.request:
            self.response_status = response.status
            self.response_event.set()


class ResumeMutationCaptureGuard:
    """Capture one exact HH mutation in memory while always aborting it."""

    def __init__(self, expected: ExpectedMutation):
        self.expected = expected
        self.payload: dict | None = None
        self.url: str | None = None
        self.error: str | None = None
        self.capture_event = asyncio.Event()
        self.commit_guard: ResumeUpdateGuard | None = None

    def arm(self, expected: ExpectedMutation, on_dispatch) -> ResumeUpdateGuard:
        if self.payload is None or self.error is not None:
            raise ValueError("Cannot arm a resume mutation before exact baseline capture")
        if self.commit_guard is not None:
            raise ValueError("Resume mutation capture guard is already armed")
        self.commit_guard = ResumeUpdateGuard(expected, on_dispatch)
        return self.commit_guard

    def observe_response(self, response):
        if self.commit_guard is not None:
            self.commit_guard.observe_response(response)

    async def handle(self, route):
        if self.commit_guard is not None:
            await self.commit_guard.handle(route)
            return
        request = route.request
        if request.method in _READ_METHODS:
            if _trusted_read(request.url):
                await route.fallback()
            else:
                await route.abort("blockedbyclient")
            return

        target = urlparse(request.url)
        candidate = (
            target.scheme == "https"
            and (target.hostname or "") == self.expected.host
            and request.method == self.expected.method
            and target.path.rstrip("/") == self.expected.path
        )
        if not candidate:
            await route.abort("blockedbyclient")
            return

        validator = ResumeUpdateGuard(self.expected, lambda: None)
        try:
            validator._validate_target(request)
            validator._validate_payload(request)
            self.payload = _parse_json(request)
            self.url = request.url
        except BaseException as error:
            self.error = str(error) or type(error).__name__
        finally:
            self.capture_event.set()
            await route.abort("blockedbyclient")


def build_captured_experience_mutation(
    resume_id: str,
    operation: dict,
    captured_payload: dict,
) -> ExpectedMutation:
    """Patch exactly one approved experience description in a captured HH payload."""
    if operation.get("kind") != "experience_description":
        raise ValueError("Captured payload patching is only supported for experience")
    payload = deepcopy(captured_payload)
    profile = payload.get("profile")
    if not isinstance(profile, dict) or set(profile) != {"experience"}:
        raise ValueError("Unexpected captured experience profile payload")
    if payload.get("resumeFromHash") != resume_id:
        raise ValueError("Captured experience payload targets a different resume")
    rows = profile.get("experience")
    if not isinstance(rows, list):
        raise ValueError("Captured experience payload is missing rows")
    matches = [
        row for row in rows
        if isinstance(row, dict) and row.get("id") == operation.get("entry_id")
    ]
    if len(matches) != 1:
        raise ValueError("Approved experience entry is missing or ambiguous")
    row = matches[0]
    if row.get("description", "") != operation.get("before"):
        raise ValueError("Experience description changed before dispatch")
    row["description"] = operation["after"]
    return ExpectedMutation(
        "POST",
        "resume-profile-front.hh.ru",
        "/profile/shards/profile/update",
        payload,
        experience_profile=True,
    )
