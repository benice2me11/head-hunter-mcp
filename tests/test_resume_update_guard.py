import json
import unittest
from unittest.mock import Mock

from hh_mcp_server.resume_update_guard import (
    ReadOnlyGuard,
    ResumeMutationCaptureGuard,
    ResumeUpdateGuard,
    build_captured_experience_mutation,
    build_expected_mutation,
)
from hh_mcp_server.resume_updates import build_resume_update_plan
from tests.test_resume_updates import RID1, owned


class FakeRequest:
    def __init__(
        self,
        method,
        url,
        payload=None,
        *,
        content_type="application/json",
    ):
        self.method = method
        self.url = url
        self.headers = {"content-type": content_type}
        self.post_data_buffer = (
            json.dumps(payload).encode() if payload is not None else b""
        )


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.action = None

    async def fallback(self):
        self.action = "fallback"

    async def abort(self, *_):
        self.action = "abort"


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_guard_blocks_every_mutating_method(self):
        guard = ReadOnlyGuard()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            route = FakeRoute(
                FakeRequest(method, "https://hh.ru/applicant/resume/edit")
            )
            await guard.handle(route)
            self.assertEqual(route.action, "abort")
        route = FakeRoute(FakeRequest("GET", "https://hh.ru/applicant/resume"))
        await guard.handle(route)
        self.assertEqual(route.action, "fallback")

    async def test_headline_guard_allows_only_exact_resume_and_position_payload(self):
        plan = build_resume_update_plan(
            RID1, {"headline": "Golang Developer"}, owned()
        )
        operation = plan["operations"][0]
        expected = build_expected_mutation(
            RID1, operation, plan["base_snapshot"]
        )
        dispatched = Mock()
        guard = ResumeUpdateGuard(expected, dispatched)

        payload = {
            **expected.payload,
            "fingerprintIteration2": "volatile-client-value",
            "fingerprintSp": "volatile-client-value-2",
        }
        route = FakeRoute(
            FakeRequest(
                "POST",
                f"https://hh.ru/applicant/resume/edit?resume={RID1}&hhtmSource=resume",
                payload,
            )
        )
        await guard.handle(route)
        self.assertEqual(route.action, "fallback")
        dispatched.assert_called_once()

        duplicate = FakeRoute(
            FakeRequest(
                "POST",
                f"https://hh.ru/applicant/resume/edit?resume={RID1}",
                payload,
            )
        )
        await guard.handle(duplicate)
        self.assertEqual(duplicate.action, "abort")

    def test_expected_partial_payload_uses_observed_hh_transport_shape(self):
        cases = [
            (
                {"headline": "Golang Developer"},
                {
                    "title": ["Golang Developer"],
                    "salary": [{"amount": 280000, "currency": "RUR"}],
                    "professionalRole": [96],
                    "travelTime": ["ANY"],
                    "businessTripReadiness": ["READY"],
                    "workFormats": ["REMOTE"],
                    "employmentForms": ["FULL"],
                },
            ),
            (
                {"about": "new"},
                {"skills": ["new"]},
            ),
            (
                {"skills": ["Go", "Docker"]},
                {"keySkills": ["Go", "Docker"]},
            ),
        ]
        for changes, expected_payload in cases:
            with self.subTest(changes=changes):
                plan = build_resume_update_plan(RID1, changes, owned())
                expected = build_expected_mutation(
                    RID1, plan["operations"][0], plan["base_snapshot"]
                )
                self.assertEqual(expected.payload, expected_payload)

    async def test_headline_guard_rejects_neighbor_change_extra_key_and_wrong_resume(self):
        plan = build_resume_update_plan(
            RID1, {"headline": "Golang Developer"}, owned()
        )
        expected = build_expected_mutation(
            RID1, plan["operations"][0], plan["base_snapshot"]
        )
        cases = []

        salary_changed = json.loads(json.dumps(expected.payload))
        salary_changed["salary"][0]["amount"] += 1
        cases.append(
            (
                f"https://hh.ru/applicant/resume/edit?resume={RID1}",
                salary_changed,
            )
        )

        extra = {**expected.payload, "accessType": [{"string": "EVERYONE"}]}
        cases.append(
            (f"https://hh.ru/applicant/resume/edit?resume={RID1}", extra)
        )
        cases.append(
            (
                "https://hh.ru/applicant/resume/edit?resume=" + "b" * 40,
                expected.payload,
            )
        )

        for url, payload in cases:
            with self.subTest(url=url):
                guard = ResumeUpdateGuard(expected, Mock())
                route = FakeRoute(FakeRequest("POST", url, payload))
                await guard.handle(route)
                self.assertEqual(route.action, "abort")
                self.assertFalse(guard.dispatched)

    async def test_experience_guard_checks_full_profile_semantically(self):
        plan = build_resume_update_plan(
            RID1,
            {"experience": [{"entry_id": 101, "description": "new"}]},
            owned(),
        )
        expected = build_expected_mutation(
            RID1, plan["operations"][0], plan["base_snapshot"]
        )
        self.assertEqual(expected.method, "POST")
        self.assertEqual(expected.host, "resume-profile-front.hh.ru")
        self.assertEqual(expected.path, "/profile/shards/profile/update")
        experience_row = expected.payload["profile"]["experience"][0]
        self.assertEqual(experience_row["professionId"], 96)
        self.assertEqual(experience_row["professionName"], "Software developer")
        self.assertNotIn("companyLogos", experience_row)
        self.assertNotIn("interval", experience_row)
        self.assertNotIn("companyAreaTitle", experience_row)

        payload = json.loads(json.dumps(expected.payload))
        payload["profile"]["experience"].reverse()
        for row in payload["profile"]["experience"]:
            if "resumes" in row:
                row["resumes"].reverse()

        guard = ResumeUpdateGuard(expected, Mock())
        route = FakeRoute(
            FakeRequest(
                "POST",
                "https://resume-profile-front.hh.ru/profile/shards/profile/update",
                payload,
            )
        )
        await guard.handle(route)
        self.assertEqual(route.action, "fallback")

        changed = json.loads(json.dumps(expected.payload))
        changed["profile"]["experience"][0]["companyName"] = "Other"
        guard = ResumeUpdateGuard(expected, Mock())
        route = FakeRoute(
            FakeRequest(
                "POST",
                "https://resume-profile-front.hh.ru/profile/shards/profile/update",
                changed,
            )
        )
        await guard.handle(route)
        self.assertEqual(route.action, "abort")

    async def test_experience_baseline_capture_is_aborted_and_patched_exactly_once(self):
        plan = build_resume_update_plan(
            RID1,
            {"experience": [{"entry_id": 101, "description": "new"}]},
            owned(),
        )
        operation = plan["operations"][0]
        baseline_operation = {**operation, "after": operation["before"]}
        baseline = build_expected_mutation(
            RID1, baseline_operation, plan["base_snapshot"]
        )
        guard = ResumeMutationCaptureGuard(baseline)
        payload = json.loads(json.dumps(baseline.payload))
        payload["profile"]["experience"].reverse()

        route = FakeRoute(
            FakeRequest(
                "POST",
                "https://resume-profile-front.hh.ru/profile/shards/profile/update",
                payload,
            )
        )
        await guard.handle(route)
        self.assertEqual(route.action, "abort")
        self.assertTrue(guard.capture_event.is_set())
        self.assertIsNone(guard.error)
        self.assertEqual(guard.payload, payload)

        final = build_captured_experience_mutation(
            RID1, operation, guard.payload
        )
        self.assertEqual(final.method, "POST")
        self.assertEqual(final.path, "/profile/shards/profile/update")
        self.assertEqual(
            [row["id"] for row in final.payload["profile"]["experience"]],
            [row["id"] for row in payload["profile"]["experience"]],
        )
        changed = [
            row for row in final.payload["profile"]["experience"]
            if row["id"] == 101
        ]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["description"], "new")
        untouched_before = [
            row for row in payload["profile"]["experience"]
            if row["id"] != 101
        ]
        untouched_after = [
            row for row in final.payload["profile"]["experience"]
            if row["id"] != 101
        ]
        self.assertEqual(untouched_after, untouched_before)

    async def test_experience_capture_rejects_changed_neighbor_state(self):
        plan = build_resume_update_plan(
            RID1,
            {"experience": [{"entry_id": 101, "description": "new"}]},
            owned(),
        )
        operation = plan["operations"][0]
        baseline_operation = {**operation, "after": operation["before"]}
        baseline = build_expected_mutation(
            RID1, baseline_operation, plan["base_snapshot"]
        )
        payload = json.loads(json.dumps(baseline.payload))
        payload["profile"]["experience"][0]["companyName"] = "Changed"
        guard = ResumeMutationCaptureGuard(baseline)
        route = FakeRoute(
            FakeRequest(
                "POST",
                "https://resume-profile-front.hh.ru/profile/shards/profile/update",
                payload,
            )
        )
        await guard.handle(route)
        self.assertEqual(route.action, "abort")
        self.assertTrue(guard.capture_event.is_set())
        self.assertIsNone(guard.payload)
        self.assertIn("differs", guard.error)

        wrong_host = ResumeMutationCaptureGuard(baseline)
        route = FakeRoute(
            FakeRequest(
                "POST",
                "https://hh.ru/profile/shards/profile/update",
                baseline.payload,
            )
        )
        await wrong_host.handle(route)
        self.assertEqual(route.action, "abort")
        self.assertFalse(wrong_host.capture_event.is_set())
        self.assertIsNone(wrong_host.payload)

    async def test_skill_level_guard_requires_exact_put_payload(self):
        plan = build_resume_update_plan(
            RID1,
            {"skill_levels": [{"name": "Go", "level": "advanced"}]},
            owned(),
        )
        expected = build_expected_mutation(
            RID1, plan["operations"][0], plan["base_snapshot"]
        )
        self.assertEqual(expected.method, "PUT")
        self.assertEqual(
            expected.path, "/shards/applicant/key_skills/levels/update"
        )
        guard = ResumeUpdateGuard(expected, Mock())
        route = FakeRoute(
            FakeRequest(
                "PUT",
                "https://hh.ru/shards/applicant/key_skills/levels/update",
                expected.payload,
            )
        )
        await guard.handle(route)
        self.assertEqual(route.action, "fallback")

        wrong = json.loads(json.dumps(expected.payload))
        wrong["userSkillLevels"][0]["level_id"] = 8
        guard = ResumeUpdateGuard(expected, Mock())
        route = FakeRoute(
            FakeRequest(
                "PUT",
                "https://hh.ru/shards/applicant/key_skills/levels/update",
                wrong,
            )
        )
        await guard.handle(route)
        self.assertEqual(route.action, "abort")

    async def test_dispatch_callback_failure_blocks_request(self):
        plan = build_resume_update_plan(RID1, {"about": "new"}, owned())
        expected = build_expected_mutation(
            RID1, plan["operations"][0], plan["base_snapshot"]
        )
        callback = Mock(side_effect=OSError("fsync failed"))
        guard = ResumeUpdateGuard(expected, callback)
        route = FakeRoute(
            FakeRequest(
                "POST",
                f"https://hh.ru/applicant/resume/edit?resume={RID1}",
                expected.payload,
            )
        )
        await guard.handle(route)
        self.assertEqual(route.action, "abort")
        self.assertFalse(guard.dispatched)


if __name__ == "__main__":
    unittest.main()
