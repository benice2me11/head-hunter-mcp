"""Typed plans, material snapshots and private drafts for resume updates."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import uuid

from hh_mcp_server.constants import PROFILE_DIR
from hh_mcp_server.drafts import content_digest
from hh_mcp_server.private_files import write_private_json
from hh_mcp_server.snapshots import normalized_text


RESUME_UPDATE_DRAFT_DIR = PROFILE_DIR / "resume-update-drafts"

ALLOWED_CHANGE_FIELDS = frozenset(
    {"headline", "about", "experience", "skills", "skill_levels"}
)

RESUME_MATERIAL_FIELDS = (
    "lang", "title", "email", "photo", "skills", "keySkills", "portfolio",
    "accessType", "travelTime", "workFormats", "hiddenFields", "setkaAccess",
    "autoHideTime", "educationLevel", "employmentForms", "professionalRole",
    "preferredContact", "businessTripReadiness", "phone", "salary", "proftest",
    "experience", "certificate", "personalSite", "recommendation",
    "primaryEducation", "additionalEducation", "elementaryEducation",
    "attestationEducation",
)

SKILL_LEVELS = {
    "base": {"level_id": 8, "level_rank": 1},
    "middle": {"level_id": 9, "level_rank": 2},
    "advanced": {"level_id": 10, "level_rank": 3},
}


def validate_resume_id(resume_id: str) -> None:
    if not isinstance(resume_id, str) or not re.fullmatch(r"[a-f0-9]{20,100}", resume_id):
        raise ValueError("Invalid resume ID")


def _skill_key(value: str) -> str:
    return normalized_text(value).casefold()


def _single_string_field(material: dict, field: str) -> str:
    value = material.get(field) or []
    if not isinstance(value, list) or len(value) > 1:
        raise ValueError(f"Resume {field} has an unsupported shape")
    if not value:
        return ""
    item = value[0]
    if not isinstance(item, dict) or set(item) != {"string"} or not isinstance(item["string"], str):
        raise ValueError(f"Resume {field} has an unsupported shape")
    return item["string"]


def _skill_names(material: dict) -> list[str]:
    result = []
    for item in material.get("keySkills") or []:
        if not isinstance(item, dict) or set(item) != {"string"}:
            raise ValueError("Resume keySkills has an unsupported shape")
        name = item["string"]
        if not isinstance(name, str) or not normalized_text(name):
            raise ValueError("Resume contains an invalid skill name")
        result.append(name)
    keys = [_skill_key(name) for name in result]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate skill names make the resume state ambiguous")
    return result


def _experience_rows(material: dict) -> list[dict]:
    rows = material.get("experience") or []
    if not isinstance(rows, list):
        raise ValueError("Resume experience has an unsupported shape")
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            raise ValueError("Resume experience entry is missing a stable numeric ID")
        if not isinstance(row.get("description", ""), str):
            raise ValueError("Resume experience description has an unsupported shape")
        ids.append(row["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate experience IDs make the resume state ambiguous")
    return rows


def _skill_levels(resume: dict) -> list[dict]:
    rows = resume.get("resumeApplicantSkills") or []
    if not isinstance(rows, list):
        raise ValueError("Resume skill level state has an unsupported shape")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("category") != "SKILL":
            continue
        skill_id = row.get("id")
        name = row.get("name")
        level = row.get("level")
        if not isinstance(skill_id, int) or not isinstance(name, str):
            raise ValueError("Resume skill level entry is missing a stable ID or name")
        if skill_id in seen:
            raise ValueError("Duplicate skill IDs make skill levels ambiguous")
        seen.add(skill_id)
        if level is None:
            level_id, level_rank, level_internal_id = 0, 0, None
        elif isinstance(level, dict):
            level_id = level.get("id")
            level_rank = level.get("rank")
            level_internal_id = level.get("internalId")
            if not isinstance(level_id, int) or not isinstance(level_rank, int):
                raise ValueError("Resume skill level has an unsupported shape")
        else:
            raise ValueError("Resume skill level has an unsupported shape")
        result.append({
            "skill_id": skill_id,
            "name": name,
            "level_id": level_id,
            "level_rank": level_rank,
            "level_internal_id": level_internal_id,
        })
    result.sort(key=lambda item: (item["skill_id"], _skill_key(item["name"])))
    return result


def _canonical_material(resume: dict) -> dict:
    material = {
        field: deepcopy(resume[field])
        for field in RESUME_MATERIAL_FIELDS
        if field in resume
    }
    _single_string_field(material, "title")
    _single_string_field(material, "skills")
    _skill_names(material)
    _experience_rows(material)
    return material


def build_resume_update_snapshot(owned_resumes: list[dict]) -> dict:
    """Return a deterministic material version for every currently owned resume."""
    if not isinstance(owned_resumes, list) or not owned_resumes:
        raise ValueError("No owned resumes were available for a material snapshot")
    resumes: dict[str, dict] = {}
    for item in owned_resumes:
        if not isinstance(item, dict):
            raise ValueError("Owned resume state has an unsupported shape")
        resume_id = item.get("id")
        validate_resume_id(resume_id)
        if resume_id in resumes:
            raise ValueError("Owned resume ID is ambiguous")
        resume = item.get("resume")
        if not isinstance(resume, dict):
            raise ValueError("Structured HH resume state is missing")
        resumes[resume_id] = {
            "material": _canonical_material(resume),
            "skill_levels": _skill_levels(resume),
        }
    fields = {"resumes": dict(sorted(resumes.items()))}
    return {"schema_version": 1, **fields, "sha256": content_digest(fields)}


def editable_resume_summary(state: dict) -> dict:
    """Return only typed fields that the resume-update workflow can address."""
    if not isinstance(state, dict):
        raise ValueError("Structured owned resume state is required")
    resume_id = state.get("id")
    validate_resume_id(resume_id)
    resume = state.get("resume")
    if not isinstance(resume, dict):
        raise ValueError("Structured HH resume state is missing")

    experience = []
    for row in _experience_rows(resume):
        values = {
            "entry_id": row["id"],
            "company": row.get("companyName", ""),
            "role": row.get("position", ""),
            "start_date": row.get("startDate"),
            "end_date": row.get("endDate"),
            "description": row.get("description", ""),
        }
        if (
            not isinstance(values["company"], str)
            or not isinstance(values["role"], str)
            or values["start_date"] is not None
            and not isinstance(values["start_date"], str)
            or values["end_date"] is not None
            and not isinstance(values["end_date"], str)
        ):
            raise ValueError("Resume experience has an unsupported editable shape")
        experience.append(values)

    level_names = {0: None, 1: "base", 2: "middle", 3: "advanced"}
    skill_levels = []
    for row in _skill_levels(resume):
        rank = row["level_rank"]
        skill_levels.append(
            {
                "skill_id": row["skill_id"],
                "name": row["name"],
                "level": level_names.get(rank, "unsupported"),
                "level_id": row["level_id"],
            }
        )

    return {
        "resume_id": resume_id,
        "headline": _single_string_field(resume, "title"),
        "about": _single_string_field(resume, "skills"),
        "experience": experience,
        "skills": _skill_names(resume),
        "skill_levels": skill_levels,
    }


def validate_resume_update_changes(changes: dict) -> dict:
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object")
    unknown = set(changes) - ALLOWED_CHANGE_FIELDS
    if unknown:
        raise ValueError(
            "Unsupported resume update field: " + ", ".join(sorted(map(str, unknown)))
        )
    normalized: dict = {}
    if "headline" in changes:
        value = changes["headline"]
        if not isinstance(value, str) or not normalized_text(value):
            raise ValueError("headline must be a non-empty string")
        normalized["headline"] = value
    if "about" in changes:
        value = changes["about"]
        if not isinstance(value, str):
            raise ValueError("about must be a string")
        normalized["about"] = value
    if "experience" in changes:
        value = changes["experience"]
        if not isinstance(value, list):
            raise ValueError("experience must be a list")
        result = []
        seen = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"entry_id", "description"}:
                raise ValueError("Experience changes must contain exactly entry_id and description")
            entry_id = item["entry_id"]
            if isinstance(entry_id, str) and entry_id.isdigit():
                entry_id = int(entry_id)
            if not isinstance(entry_id, int) or entry_id <= 0:
                raise ValueError("Experience entry_id must be a positive integer")
            if entry_id in seen:
                raise ValueError("Ambiguous experience: duplicate entry_id in changes")
            if not isinstance(item["description"], str):
                raise ValueError("Experience description must be a string")
            seen.add(entry_id)
            result.append({"entry_id": entry_id, "description": item["description"]})
        normalized["experience"] = sorted(result, key=lambda item: item["entry_id"])
    if "skills" in changes:
        value = changes["skills"]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("skills must be a list of strings")
        if any(not normalized_text(item) for item in value):
            raise ValueError("Skill names cannot be empty")
        keys = [_skill_key(item) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate skill names are not allowed")
        normalized["skills"] = list(value)
    if "skill_levels" in changes:
        value = changes["skill_levels"]
        if not isinstance(value, list):
            raise ValueError("skill_levels must be a list")
        result = []
        seen = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"name", "level"}:
                raise ValueError("Each skill level change must contain exactly name and level")
            name = item["name"]
            level = item["level"]
            if not isinstance(name, str) or not normalized_text(name):
                raise ValueError("Skill level name cannot be empty")
            if level not in SKILL_LEVELS:
                raise ValueError("Unsupported skill level")
            key = _skill_key(name)
            if key in seen:
                raise ValueError("Duplicate skill level changes are ambiguous")
            seen.add(key)
            result.append({"name": name, "level": level})
        normalized["skill_levels"] = sorted(result, key=lambda item: _skill_key(item["name"]))
    return normalized


def _snapshot_with_digest(snapshot: dict) -> dict:
    fields = {"resumes": deepcopy(snapshot["resumes"])}
    return {"schema_version": 1, **fields, "sha256": content_digest(fields)}


def _find_experience(snapshot: dict, entry_id: int) -> list[tuple[str, dict]]:
    result = []
    for resume_id, state in snapshot["resumes"].items():
        matches = [
            item for item in state["material"].get("experience", [])
            if item.get("id") == entry_id
        ]
        if len(matches) > 1:
            raise ValueError("Ambiguous experience entry in owned resumes")
        if matches:
            result.append((resume_id, matches[0]))
    return result


def _experience_identity(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "description"}


def _find_skill_levels(snapshot: dict, name: str) -> list[tuple[str, dict]]:
    key = _skill_key(name)
    result = []
    for resume_id, state in snapshot["resumes"].items():
        matches = [
            item for item in state["skill_levels"]
            if _skill_key(item["name"]) == key
        ]
        if len(matches) > 1:
            raise ValueError("Ambiguous skill level entry in owned resumes")
        if matches:
            result.append((resume_id, matches[0]))
    return result


def build_resume_update_plan(
    resume_id: str, changes: dict, owned_resumes: list[dict]
) -> dict:
    validate_resume_id(resume_id)
    normalized = validate_resume_update_changes(changes)
    owned_matches = [item for item in owned_resumes if item.get("id") == resume_id]
    if len(owned_matches) != 1:
        raise ValueError("The selected resume was not uniquely found in your own resumes")

    base = build_resume_update_snapshot(owned_resumes)
    expected = deepcopy(base)
    target = expected["resumes"][resume_id]
    base_target = base["resumes"][resume_id]
    preview: dict = {}
    operations: list[dict] = []

    if "headline" in normalized:
        before = _single_string_field(base_target["material"], "title")
        after = normalized["headline"]
        if before != after:
            target["material"]["title"] = [{"string": after}]
            preview["headline"] = {"before": before, "after": after}
            operations.append({
                "kind": "headline", "resume_id": resume_id,
                "before": before, "after": after,
            })

    for change in normalized.get("experience", []):
        matches = _find_experience(base, change["entry_id"])
        if not matches or not any(rid == resume_id for rid, _ in matches):
            raise ValueError(
                f"Experience entry {change['entry_id']} is unknown for the selected resume"
            )
        identities = {
            json.dumps(
                _experience_identity(row), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
            for _, row in matches
        }
        descriptions = {row["description"] for _, row in matches}
        if len(identities) != 1 or len(descriptions) != 1:
            raise ValueError("Ambiguous experience: shared entry state is inconsistent")
        before = next(iter(descriptions))
        after = change["description"]
        if before == after:
            continue
        affected = sorted(rid for rid, _ in matches)
        for affected_id in affected:
            rows = expected["resumes"][affected_id]["material"]["experience"]
            row = next(item for item in rows if item["id"] == change["entry_id"])
            row["description"] = after
        item = {
            "entry_id": change["entry_id"], "before": before, "after": after,
            "affected_resume_ids": affected,
        }
        preview.setdefault("experience", []).append(deepcopy(item))
        operations.append({"kind": "experience_description", **item})

    if "about" in normalized:
        before = _single_string_field(base_target["material"], "skills")
        after = normalized["about"]
        if before != after:
            target["material"]["skills"] = [{"string": after}] if after else []
            preview["about"] = {"before": before, "after": after}
            operations.append({
                "kind": "about", "resume_id": resume_id,
                "before": before, "after": after,
            })

    if "skills" in normalized:
        before = _skill_names(base_target["material"])
        after = normalized["skills"]
        if before != after:
            target["material"]["keySkills"] = [{"string": name} for name in after]
            preview["skills"] = {"before": before, "after": after}
            operations.append({
                "kind": "skills", "resume_id": resume_id,
                "before": before, "after": deepcopy(after),
            })

    for change in normalized.get("skill_levels", []):
        base_skill_names = {
            _skill_key(name) for name in _skill_names(base_target["material"])
        }
        final_skill_names = {
            _skill_key(name) for name in _skill_names(target["material"])
        }
        skill_key = _skill_key(change["name"])
        if skill_key not in base_skill_names or skill_key not in final_skill_names:
            raise ValueError(
                f"Skill level can only be set for an existing selected skill: {change['name']}"
            )
        matches = _find_skill_levels(base, change["name"])
        target_matches = [item for rid, item in matches if rid == resume_id]
        if len(target_matches) != 1:
            raise ValueError(f"Skill level is not uniquely known: {change['name']}")
        skill_ids = {item["skill_id"] for _, item in matches}
        levels = {(item["level_id"], item["level_rank"]) for _, item in matches}
        if len(skill_ids) != 1 or len(levels) != 1:
            raise ValueError(f"Skill level state is ambiguous: {change['name']}")
        desired = SKILL_LEVELS[change["level"]]
        before_level_id, before_rank = next(iter(levels))
        if (before_level_id, before_rank) == (
            desired["level_id"], desired["level_rank"]
        ):
            continue
        affected = sorted(rid for rid, _ in matches)
        for affected_id, current in matches:
            rows = expected["resumes"][affected_id]["skill_levels"]
            row = next(item for item in rows if item["skill_id"] == current["skill_id"])
            row["level_id"] = desired["level_id"]
            row["level_rank"] = desired["level_rank"]
            row["level_internal_id"] = change["level"]
        before_name = {1: "base", 2: "middle", 3: "advanced"}.get(before_rank, "unrated")
        item = {
            "name": change["name"], "skill_id": next(iter(skill_ids)),
            "before": before_name, "after": change["level"],
            "before_level_id": before_level_id,
            "after_level_id": desired["level_id"],
            "affected_resume_ids": affected,
        }
        preview.setdefault("skill_levels", []).append(deepcopy(item))
        operations.append({"kind": "skill_level", **item})

    if not operations:
        return {
            "status": "no_changes", "resume_id": resume_id,
            "preview": {}, "base_snapshot": base,
        }

    expected = _snapshot_with_digest(expected)
    content = {
        "content_schema": 2,
        "resume_id": resume_id,
        "changes": normalized,
        "preview": preview,
        "operations": operations,
        "base_snapshot": base,
        "expected_snapshot": expected,
    }
    return {
        "status": "prepared", "resume_id": resume_id,
        "preview": preview, "operations": operations,
        "base_snapshot": base, "expected_snapshot": expected,
        "content": content,
    }


def resume_update_draft_path(draft_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", draft_id):
        raise ValueError("Invalid draft ID")
    return RESUME_UPDATE_DRAFT_DIR / f"{draft_id}.json"


def save_resume_update_draft(draft: dict) -> None:
    write_private_json(resume_update_draft_path(draft["draft_id"]), draft)


def create_resume_update_draft(plan: dict) -> dict:
    if plan.get("status") != "prepared" or not plan.get("operations"):
        raise ValueError("A no-op resume update must not create a draft")
    content = deepcopy(plan["content"])
    validate_resume_id(content["resume_id"])
    draft = {
        "draft_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "prepared",
        "digest": content_digest(content),
        "content": content,
        "completed_operations": [],
        "last_result": None,
    }
    save_resume_update_draft(draft)
    return draft


def load_resume_update_draft(draft_id: str) -> dict:
    return json.loads(resume_update_draft_path(draft_id).read_text(encoding="utf-8"))


def load_confirmed_resume_update_draft(
    draft_id: str,
    approved_digest: str,
    confirmed: bool,
    *,
    allow_reconciliation: bool = False,
) -> dict:
    if confirmed is not True:
        raise ValueError("User confirmation of this exact resume update is required")
    draft = load_resume_update_draft(draft_id)
    if draft.get("digest") != approved_digest:
        raise ValueError("Approved digest does not match the resume update draft")
    if content_digest(draft.get("content", {})) != approved_digest:
        raise ValueError("Resume update content changed after review")
    age = datetime.now(timezone.utc) - datetime.fromisoformat(draft["created_at"])
    if not timedelta(0) <= age < timedelta(hours=24):
        raise ValueError("Resume update draft expired; prepare and review a fresh draft")
    validate_resume_id(draft["content"].get("resume_id", ""))
    status = draft.get("status")
    if status == "cancelled":
        raise ValueError("Resume update draft was cancelled")
    if status != "prepared" and not (
        allow_reconciliation
        and status in {
            "unverified",
            "partial_success",
            "dispatched_unverified",
            "in_progress",
        }
    ):
        raise ValueError(
            "Resume update draft is no longer writable; reconcile or prepare a new draft"
        )
    return draft


def _snapshot_with_operation(snapshot: dict, operation: dict) -> dict:
    result = deepcopy(snapshot)
    kind = operation["kind"]
    if kind in {"headline", "about", "skills"}:
        material = result["resumes"][operation["resume_id"]]["material"]
        if kind == "headline":
            material["title"] = [{"string": operation["after"]}]
        elif kind == "about":
            material["skills"] = (
                [{"string": operation["after"]}] if operation["after"] else []
            )
        else:
            material["keySkills"] = [
                {"string": name} for name in operation["after"]
            ]
    elif kind == "experience_description":
        for affected_id in operation["affected_resume_ids"]:
            rows = result["resumes"][affected_id]["material"]["experience"]
            matches = [row for row in rows if row["id"] == operation["entry_id"]]
            if len(matches) != 1:
                raise ValueError("Approved experience entry is no longer unique")
            matches[0]["description"] = operation["after"]
    elif kind == "skill_level":
        desired = SKILL_LEVELS[operation["after"]]
        for affected_id in operation["affected_resume_ids"]:
            rows = result["resumes"][affected_id]["skill_levels"]
            matches = [
                row for row in rows if row["skill_id"] == operation["skill_id"]
            ]
            if len(matches) != 1:
                raise ValueError("Approved skill level entry is no longer unique")
            matches[0]["level_id"] = desired["level_id"]
            matches[0]["level_rank"] = desired["level_rank"]
            matches[0]["level_internal_id"] = operation["after"]
    else:
        raise ValueError(f"Unsupported resume update operation: {kind}")
    return _snapshot_with_digest(result)


def expected_prefix_snapshots(base_snapshot: dict, operations: list[dict]) -> list[dict]:
    snapshots = [deepcopy(base_snapshot)]
    current = deepcopy(base_snapshot)
    for operation in operations:
        current = _snapshot_with_operation(current, operation)
        snapshots.append(current)
    return snapshots
