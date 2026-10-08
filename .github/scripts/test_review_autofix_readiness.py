import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import review_autofix_readiness as readiness


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.pr = {"number": 287, "head": {"sha": "current"}}
        self.profile = {"required_completion_checks": ["Tests", "Greptile Review", "Seer Code Review"],
                        "review_check_names": ["Greptile Review", "Seer Code Review"]}
        self.checks = [{"name": name, "status": "completed", "conclusion": "success"}
                       for name in self.profile["required_completion_checks"]]
        self.statuses = []
        self.runs = []
        self.reviews = []
        self.previous_checks = []

    def pages(self, path):
        self.assertEqual(path, "pulls/287/reviews")
        return self.reviews

    def get(self, path):
        route, query = path.split("?", 1)
        params = parse_qs(query)
        page = int(params["page"][0])
        if route == "commits/reviewed/check-runs":
            key, items = "check_runs", self.previous_checks
        elif route == "commits/current/check-runs":
            self.assertEqual(params["filter"], ["latest"])
            key, items = "check_runs", self.checks
        elif route == "commits/current/status":
            key, items = "statuses", self.statuses
        elif route == "actions/runs":
            self.assertEqual(params["head_sha"], ["current"])
            self.assertIn(params["status"][0], readiness.ACTIVE_STATUSES)
            key, items = "workflow_runs", [r for r in self.runs if r["status"] == params["status"][0]]
        else:
            raise AssertionError(path)
        return {"total_count": len(items), key: items[(page-1)*100:page*100]}

    def inspect(self):
        return readiness.inspect(self, self.pr, self.profile)

    def test_completed_checks_include_failures_and_explicitly_skipped_reviews(self):
        for conclusion in ["success", "failure", "timed_out", "skipped", "neutral"]:
            self.checks[-1]["conclusion"] = conclusion
            self.assertIsNone(self.inspect()[1])

    def test_cancelled_stale_and_incomplete_required_reviews_wait_for_rerun(self):
        for conclusion in ["cancelled", "stale", "action_required", "startup_failure", None]:
            self.checks[-1]["conclusion"] = conclusion
            self.assertIn("rerun", self.inspect()[1])

    def test_empty_or_not_yet_registered_review_check_is_not_ready(self):
        self.checks.pop()
        self.assertIn("Seer Code Review", self.inspect()[1])
        self.checks.clear()
        self.assertIn("waiting", self.inspect()[1])

    def test_pending_optional_check_or_commit_status_also_blocks(self):
        self.checks.append({"name": "Additional scan", "status": "queued"})
        self.assertIn("waiting", self.inspect()[1])
        self.checks.pop()
        self.statuses = [{"context": "External", "state": "pending"}]
        self.assertIn("commit statuses", self.inspect()[1])

    def test_reviewer_may_report_via_commit_status(self):
        self.checks.pop()
        self.statuses = [{"context": "Seer Code Review", "state": "success"}]
        self.assertIsNone(self.inspect()[1])

    def test_queued_workflow_blocks_even_before_its_jobs_register(self):
        for status in readiness.ACTIVE_STATUSES:
            self.runs = [{"head_sha": "current", "path": ".github/workflows/tests.yml", "status": status}]
            self.assertIn("workflow runs", self.inspect()[1])
            self.runs[0]["head_sha"] = "old"
            self.assertIsNone(self.inspect()[1])

    def test_promotion_does_not_wait_for_its_own_controller_jobs(self):
        self.runs = [{"head_sha": "current", "path": readiness.CONTROLLER_PATH, "status": "in_progress", "check_suite_id": 123}]
        self.checks.append({"name": "fix", "status": "in_progress", "check_suite": {"id": 123}, "app": {"slug": "github-actions"}})
        checks, waiting = self.inspect()
        self.assertIsNone(waiting)
        self.assertNotIn("fix", [check["name"] for check in checks])
        self.checks[-1]["app"]["slug"] = "untrusted-app"
        self.assertIn("waiting", self.inspect()[1])

    def test_other_workflow_with_controller_job_name_is_not_ignored(self):
        self.runs = [{"head_sha": "current", "path": ".github/workflows/tests.yml", "status": "in_progress", "check_suite_id": 123}]
        self.checks.append({"name": "fix", "status": "in_progress", "check_suite": {"id": 123}, "app": {"slug": "github-actions"}})
        self.assertIn("waiting", self.inspect()[1])

    def test_completed_controller_history_cannot_exhaust_scan(self):
        self.runs = [{"head_sha": "current", "path": readiness.CONTROLLER_PATH,
                      "status": "completed"}] * 10001
        self.assertIsNone(self.inspect()[1])

    def test_pending_workflow_after_first_active_page_is_not_missed(self):
        self.runs = [{"head_sha": "current", "path": readiness.CONTROLLER_PATH,
                      "status": "queued", "check_suite_id": i} for i in range(100)]
        self.runs.append({"head_sha": "current", "path": ".github/workflows/tests.yml", "status": "queued"})
        self.assertIn("workflow runs", self.inspect()[1])
        self.runs[-1]["status"] = "completed"
        self.assertIsNone(self.inspect()[1])

    def test_current_checks_and_statuses_beyond_first_page_are_read(self):
        self.checks += [{"name": f"Extra {i}", "status": "completed", "conclusion": "success"} for i in range(100)]
        self.statuses = [{"context": f"Extra {i}", "state": "success"} for i in range(101)]
        self.assertIsNone(self.inspect()[1])
        self.statuses[-1]["state"] = "pending"
        self.assertIn("commit statuses", self.inspect()[1])
        self.statuses[-1]["state"] = "success"
        self.checks[-1]["status"] = "queued"
        self.assertIn("waiting", self.inspect()[1])

    def test_stale_api_count_does_not_fail_a_readiness_scan(self):
        original_get = self.get
        def stale_counts(path):
            response = original_get(path)
            response["total_count"] = 50000
            return response
        with patch.object(self, "get", side_effect=stale_counts):
            self.assertIsNone(self.inspect()[1])
            self.runs = [{"head_sha": "current", "path": ".github/workflows/tests.yml", "status": "queued"}]
            self.assertIn("workflow runs", self.inspect()[1])

    def test_initial_completed_pr_reviews_are_not_required_again_after_a_push(self):
        self.previous_checks = self.checks[1:]
        self.checks = self.checks[:1]
        self.reviews = [{"user": {"login": "greptile-apps[bot]"}, "state": "COMMENTED", "commit_id": "reviewed"}]
        self.assertIsNone(self.inspect()[1])
        self.previous_checks[-1]["status"] = "in_progress"
        self.assertIn("Seer Code Review", self.inspect()[1])
        self.previous_checks[-1]["status"] = "completed"
        self.reviews[0]["user"]["login"] = "untrusted-user"
        self.assertIn("waiting", self.inspect()[1])
        self.reviews[0]["user"]["login"] = "greptile-apps[bot]"
        self.pr["created_at"] = "2026-10-02T04:00:00Z"
        for check in self.previous_checks:
            check["completed_at"] = "2026-10-02T03:00:00Z"
        self.assertIn("waiting", self.inspect()[1])
        for check in self.previous_checks:
            check["completed_at"] = "2026-10-02T04:01:00Z"
        self.assertIsNone(self.inspect()[1])

    def test_later_attempts_need_current_ci_but_do_not_force_another_review(self):
        self.checks = self.checks[:1]
        self.assertIsNone(readiness.inspect(self, self.pr, self.profile, require_reviews=False)[1])
        self.checks[0]["status"] = "queued"
        self.assertIn("waiting", readiness.inspect(self, self.pr, self.profile, require_reviews=False)[1])


if __name__ == "__main__":
    unittest.main()
