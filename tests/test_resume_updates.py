from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch
import unittest

from hh_mcp_server import resume_updates
from hh_mcp_server.resume_updates import (
    build_resume_update_plan,
    build_resume_update_snapshot,
    create_resume_update_draft,
    editable_resume_summary,
    load_confirmed_resume_update_draft,
    load_resume_update_draft,
)
from hh_mcp_server.tools.resume import register_resume_tools


RID1 = "a" * 40
RID2 = "b" * 40


def experience(entry_id=101, description="old description"):
    return {
        "id": entry_id,
        "position": "Backend Engineer",
        "description": description,
        "startDate": "2024-01-01",
        "endDate": "2026-01-01",
        "companyName": "Acme",
        "companyUrl": "",
        "companyIndustries": [],
        "companyIndustryId": None,
        "companyAreaId": 1,
        "companyAreaTitle": "Fixture City",
        "companyId": None,
        "industries": [],
        "employerId": None,
        "companyState": None,
        "verification": {"canModify": True, "status": "NOT_VERIFIED"},
        "professionId": 96,
        "professionName": "Software developer",
        "companyLogos": None,
        "interval": {"from": "2024-01", "to": "2026-01"},
    }


def resume(title):
    return {
        "lang": "RU",
        "title": [{"string": title}],
        "email": [{"string": "fixture@example.invalid"}],
        "skills": [{"string": "about"}],
        "keySkills": [{"string": "Go"}, {"string": "PostgreSQL"}],
        "accessType": [{"string": "EVERYONE"}],
        "travelTime": [{"string": "ANY"}],
        "workFormats": [{"string": "REMOTE"}],
        "employmentForms": [{"string": "FULL"}],
        "professionalRole": [{"string": 96}],
        "businessTripReadiness": [{"string": "READY"}],
        "salary": [{"amount": 280000, "currency": "RUR"}],
        "experience": [experience()],
        "resumeApplicantSkills": [
            {
                "category": "SKILL",
                "id": 501,
                "name": "Go",
                "level": {
                    "id": 9,
                    "rank": 2,
                    "internalId": "middle",
                    "name": "Middle",
                },
            },
            {
                "category": "SKILL",
                "id": 502,
                "name": "PostgreSQL",
                "level": None,
            },
        ],
    }


def owned():
    return [
        {"id": RID1, "title": "Resume One", "resume": resume("Resume One")},
        {"id": RID2, "title": "Resume Two", "resume": resume("Resume Two")},
    ]


class ToolRegistry:
    def __init__(self):
        self.methods = {}

    def tool(self, *args, **kwargs):
        def register(function):
            self.methods[function.__name__] = function
            return function
        return register


class ResumeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_resume_adds_editable_ids_for_owned_resume(self):
        registry = ToolRegistry()
        register_resume_tools(registry)
        state = {"id": RID1, "title": "Resume One", "resume": resume("Resume One")}
        raw = {"id": RID1, "url": f"https://hh.ru/resume/{RID1}", "raw_text": "raw"}
        with (
            patch("hh_mcp_server.tools.resume.get_page", new=AsyncMock(return_value=object())),
            patch("hh_mcp_server.tools.resume.ensure_authenticated", new=AsyncMock()),
            patch(
                "hh_mcp_server.tools.resume._get_resumes",
                new=AsyncMock(return_value=[{"id": RID1, "title": "Resume One"}]),
            ),
            patch(
                "hh_mcp_server.tools.resume.parse_resume_page",
                new=AsyncMock(return_value=raw),
            ),
            patch(
                "hh_mcp_server.tools.resume.read_owned_resume_states",
                new=AsyncMock(return_value=[state]),
            ) as structured,
        ):
            result = await registry.methods["get_resume"](RID1)

        self.assertEqual(result["raw_text"], "raw")
        self.assertEqual(result["editable"]["experience"][0]["entry_id"], 101)
        self.assertEqual(result["editable"]["skill_levels"][0]["skill_id"], 501)
        structured.assert_awaited_once()

    async def test_get_resume_rejects_unknown_or_ambiguous_owned_resume(self):
        registry = ToolRegistry()
        register_resume_tools(registry)
        with (
            patch("hh_mcp_server.tools.resume.get_page", new=AsyncMock(return_value=object())),
            patch("hh_mcp_server.tools.resume.ensure_authenticated", new=AsyncMock()),
            patch(
                "hh_mcp_server.tools.resume._get_resumes",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "hh_mcp_server.tools.resume.parse_resume_page",
                new=AsyncMock(side_effect=AssertionError("must not read foreign resume")),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "own resumes"):
                await registry.methods["get_resume"](RID1)


class ResumeUpdatePlanTests(unittest.TestCase):
    def test_editable_summary_exposes_stable_ids_without_unrelated_fields(self):
        state = {"id": RID1, "title": "Resume One", "resume": resume("Resume One")}
        summary = editable_resume_summary(state)
        self.assertEqual(summary["headline"], "Resume One")
        self.assertEqual(summary["about"], "about")
        self.assertEqual(summary["skills"], ["Go", "PostgreSQL"])
        self.assertEqual(
            summary["experience"],
            [
                {
                    "entry_id": 101,
                    "company": "Acme",
                    "role": "Backend Engineer",
                    "start_date": "2024-01-01",
                    "end_date": "2026-01-01",
                    "description": "old description",
                }
            ],
        )
        self.assertEqual(summary["skill_levels"][0]["skill_id"], 501)
        self.assertEqual(summary["skill_levels"][0]["name"], "Go")
        self.assertEqual(summary["skill_levels"][0]["level"], "middle")
        self.assertNotIn("salary", summary)
        self.assertNotIn("email", summary)
        self.assertNotIn("accessType", summary)

    def test_snapshot_is_deterministic_and_material(self):
        first = build_resume_update_snapshot(owned())
        second_states = list(reversed(owned()))
        second = build_resume_update_snapshot(second_states)
        self.assertEqual(first, second)
        self.assertEqual(set(first["resumes"]), {RID1, RID2})
        self.assertEqual(
            first["resumes"][RID1]["material"]["experience"][0]["id"],
            101,
        )

    def test_own_resume_is_prepared_with_exact_preview(self):
        plan = build_resume_update_plan(
            RID1,
            {
                "headline": "Golang Developer",
                "experience": [{"entry_id": 101, "description": "approved"}],
                "about": "new about",
                "skills": ["Go", "Docker"],
            },
            owned(),
        )
        self.assertEqual(plan["status"], "prepared")
        self.assertEqual(plan["preview"]["headline"], {
            "before": "Resume One",
            "after": "Golang Developer",
        })
        self.assertEqual(plan["preview"]["about"], {
            "before": "about",
            "after": "new about",
        })
        self.assertEqual(
            plan["preview"]["experience"][0]["affected_resume_ids"],
            [RID1, RID2],
        )
        self.assertEqual(
            [item["kind"] for item in plan["operations"]],
            ["headline", "experience_description", "about", "skills"],
        )

    def test_unknown_foreign_resume_and_unknown_field_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "own resumes"):
            build_resume_update_plan("c" * 40, {"about": "x"}, owned())
        for changes in (
            {"salary": "300000"},
            {"contacts": {"email": "other@example.invalid"}},
            {"selector": "[data-qa=anything]"},
            {"url": "https://example.invalid"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_resume_update_plan(RID1, changes, owned())

    def test_noop_does_not_create_operations(self):
        plan = build_resume_update_plan(
            RID1,
            {
                "headline": "Resume One",
                "about": "about",
                "experience": [{"entry_id": 101, "description": "old description"}],
                "skills": ["Go", "PostgreSQL"],
                "skill_levels": [{"name": "Go", "level": "middle"}],
            },
            owned(),
        )
        self.assertEqual(plan["status"], "no_changes")
        self.assertEqual(plan["preview"], {})

    def test_ambiguous_or_unknown_experience_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            build_resume_update_plan(
                RID1,
                {"experience": [{"entry_id": 999, "description": "changed"}]},
                owned(),
            )
        states = owned()
        duplicate = deepcopy(states[0]["resume"]["experience"][0])
        states[0]["resume"]["experience"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            build_resume_update_plan(
                RID1,
                {"experience": [{"entry_id": 101, "description": "changed"}]},
                states,
            )

    def test_skill_level_mapping_is_typed_and_shared(self):
        plan = build_resume_update_plan(
            RID1,
            {"skill_levels": [{"name": "Go", "level": "advanced"}]},
            owned(),
        )
        operation = plan["operations"][0]
        self.assertEqual(operation["kind"], "skill_level")
        self.assertEqual(operation["skill_id"], 501)
        self.assertEqual(operation["before_level_id"], 9)
        self.assertEqual(operation["after_level_id"], 10)
        self.assertEqual(operation["affected_resume_ids"], [RID1, RID2])


class ResumeUpdateDraftTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name) / "drafts"
        self.patcher = patch.object(
            resume_updates, "RESUME_UPDATE_DRAFT_DIR", self.folder
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def create(self):
        plan = build_resume_update_plan(RID1, {"about": "new"}, owned())
        return create_resume_update_draft(plan)

    def test_digest_is_deterministic_and_tampering_is_rejected(self):
        draft = self.create()
        stored = load_resume_update_draft(draft["draft_id"])
        self.assertEqual(stored["digest"], draft["digest"])
        stored["content"]["changes"]["about"] = "tampered"
        resume_updates.save_resume_update_draft(stored)
        with self.assertRaisesRegex(ValueError, "changed"):
            load_confirmed_resume_update_draft(
                draft["draft_id"], draft["digest"], True
            )

    def test_confirmation_wrong_digest_expired_and_cancelled_are_rejected(self):
        draft = self.create()
        with self.assertRaisesRegex(ValueError, "confirmation"):
            load_confirmed_resume_update_draft(
                draft["draft_id"], draft["digest"], False
            )
        with self.assertRaisesRegex(ValueError, "digest"):
            load_confirmed_resume_update_draft(
                draft["draft_id"], "0" * 64, True
            )

        stored = load_resume_update_draft(draft["draft_id"])
        stored["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
        resume_updates.save_resume_update_draft(stored)
        with self.assertRaisesRegex(ValueError, "expired"):
            load_confirmed_resume_update_draft(
                draft["draft_id"], draft["digest"], True
            )

        fresh = self.create()
        stored = load_resume_update_draft(fresh["draft_id"])
        stored["status"] = "cancelled"
        resume_updates.save_resume_update_draft(stored)
        with self.assertRaisesRegex(ValueError, "cancelled"):
            load_confirmed_resume_update_draft(
                fresh["draft_id"], fresh["digest"], True
            )


if __name__ == "__main__":
    unittest.main()
