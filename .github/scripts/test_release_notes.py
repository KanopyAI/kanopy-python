"""Release workflow regressions: provenance, omissions, stale reviews and gating."""

from datetime import date
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import release_notes as rn
import release_notes_controller as controller


def entry(**values):
    result = {"id": "evidence-download-fix", "source_digest": "a" * 64, "type": "fixed",
              "source_files": [],
              "summary": "Restore evidence downloads.", "audience": "API integrations",
              "customer_action": "None.", "api": {"impact": "compatible",
              "assessment": "Existing API keys can read their authorized evidence again.",
              "endpoints": ["GET /api/jobs/{id}/asset/{path}"]},
              "data_effect": "Existing results do not change.", "availability": "on_deploy", "rollout": "After successful production deployment.",
              "dependencies": [], "notice_url": "", "notice_date": "", "effective_date": ""}
    result.update(values)
    return result


def manifest(**values):
    result = {"schema_version": 1, "id": "backend-pr-304", "repository": "KanopyAI/kanopy-backend",
              "promotion_pr": "https://github.com/KanopyAI/kanopy-backend/pull/304",
              "state": "upcoming", "base_sha": "b" * 40, "head_sha": "c" * 40,
              "deployment": {"verified": False, "completed_at": ""}, "entries": [entry()]}
    result.update(values)
    return result


def developer_notice(**values):
    result = {"publish": True, "reason": "Restores API result downloads.",
              "summary": "API keys can download job results again when they have access to the job.",
              "action": "", "availability": "", "data_effect": ""}
    result.update(values)
    return result


class EntryTests(unittest.TestCase):
    def test_explicit_no_api_impact_is_allowed_but_must_explain(self):
        record = entry(type="internal", api={"impact": "none", "endpoints": [],
                                             "assessment": "Changes only the release controller, not application behavior."})
        rn.validate_entry(record)
        record["api"]["assessment"] = "none"
        with self.assertRaises(ValueError):
            rn.validate_entry(record)

    def test_api_change_cannot_be_hidden_as_internal(self):
        with self.assertRaisesRegex(ValueError, "internal"):
            rn.validate_entry(entry(type="internal"))

    def test_unknown_fields_and_missing_fields_rejected(self):
        for record in (entry(approved=True), {k: v for k, v in entry().items() if k != "customer_action"}):
            with self.assertRaises(ValueError):
                rn.validate_entry(record)

    def test_coordinated_release_needs_dependencies(self):
        with self.assertRaisesRegex(ValueError, "dependencies"):
            rn.validate_entry(entry(availability="coordinated"))

    def test_breaking_change_requires_actual_migration_details(self):
        record = entry()
        record["api"]["impact"] = "breaking"
        with self.assertRaisesRegex(ValueError, "notice"):
            rn.validate_entry(record)
        record.update(notice_url="https://status.kanopy-ai.com/changelog.html#upcoming",
                      notice_date="2026-10-01", effective_date="2026-11-01")
        with self.assertRaisesRegex(ValueError, "migration"):
            rn.validate_entry(record)
        record["customer_action"] = "Switch clients to the replacement route before November."
        rn.validate_entry(record)  # Can develop/test in staging before the date.
        with self.assertRaisesRegex(ValueError, "Cannot deploy"):
            rn.validate_entry(record, release=True, today=date(2026, 10, 6))
        rn.validate_entry(record, release=True, today=date(2026, 11, 1))

    def test_deprecation_has_same_notice_guard(self):
        with self.assertRaisesRegex(ValueError, "notice"):
            rn.validate_entry(entry(type="deprecated"))

    def test_fake_notice_host_and_same_day_notice_rejected(self):
        record = entry(notice_url="https://status.kanopy-ai.com.evil.test/",
                       notice_date="2026-10-01", effective_date="2026-11-01",
                       customer_action="Migrate to the replacement.")
        with self.assertRaises(ValueError):
            rn.validate_entry(record)
        record.update(notice_url="https://status.kanopy-ai.com/changelog.html", effective_date="2026-10-01")
        with self.assertRaisesRegex(ValueError, "precede"):
            rn.validate_entry(record)


class RenderingTests(unittest.TestCase):
    def test_customer_notice_excludes_private_assessment_and_boilerplate(self):
        record = entry(developer_notice=developer_notice(),
                       summary="Repair internal worker routing and retry thresholds.",
                       audience="API integrations", data_effect="Existing results do not change.")
        rn.validate_entry(record)
        change = rn.public_change(record, deployed=True)
        self.assertEqual(change["body"], record["developer_notice"]["summary"])
        self.assertEqual(change["customer_action"], "")
        self.assertEqual(change["data_effect"], "")
        self.assertEqual(change["availability"], "")
        self.assertEqual(change["audience"], "")
        self.assertNotIn("worker", json.dumps(change))
        self.assertIn("worker", rn.render_brief(manifest(entries=[record])))

    def test_ui_only_and_insignificant_changes_produce_no_release(self):
        ui = entry(type="improved", api={"impact": "none", "assessment": "Sidebar spacing only; no API impact.", "endpoints": []},
                   developer_notice=developer_notice(publish=False, reason="UI-only change.", summary=""))
        minor = entry(developer_notice=developer_notice(publish=False, reason="Negligible performance optimization.", summary=""))
        for record in (ui, minor):
            rn.validate_entry(record)
        for state in ("upcoming", "deployed"):
            record = manifest(state=state, entries=[ui, minor], deployment={"verified": True, "completed_at": "2026-10-08T00:00:00Z"})
            self.assertEqual(rn.update_public(record, {"releases": []}, {"releases": []}), ({"releases": []}, {"releases": []}))
        self.assertIn("UI-only", rn.render_brief(record))

    def test_legacy_ui_assessment_does_not_create_new_customer_noise(self):
        ui = entry(api={"impact": "none", "assessment": "Only UI spacing changes, no API effect.", "endpoints": []})
        rn.validate_entry(ui)
        self.assertFalse(rn.customer_visible(ui))
        self.assertTrue(rn.customer_visible(entry()))

    def test_new_policy_preserves_published_history_verbatim(self):
        old = {"releases": [{"version": "2026.10.1", "date": "2026-10-01", "source_id": "backend-pr-304",
                             "changes": [{"type": "improved", "body": "Original detailed UI update."}]}]}
        record = manifest(entries=[entry(developer_notice=developer_notice(publish=False, summary=""))])
        _, released = rn.update_public(record, {"releases": []}, old)
        self.assertEqual(released, old)

    def test_notice_keeps_customer_consequences_without_private_dependencies(self):
        record = entry(availability="coordinated", dependencies=["Worker deploy and queue flag"],
                       customer_action="Reprocess affected jobs.",
                       developer_notice=developer_notice(action="Reprocess affected jobs.",
                           availability="Not yet enabled; release date unconfirmed.",
                           data_effect="Previously processed results are unchanged until reprocessed."))
        rn.validate_entry(record)
        change = rn.public_change(record, deployed=True)
        self.assertEqual(change["availability"], record["developer_notice"]["availability"])
        self.assertEqual(change["customer_action"], "Reprocess affected jobs.")
        self.assertTrue(change["data_effect"])
        self.assertEqual(change["dependencies"], [])
        for field in ("action", "availability"):
            invalid = json.loads(json.dumps(record))
            invalid["developer_notice"][field] = ""
            with self.assertRaises(ValueError):
                rn.validate_entry(invalid)

    def test_behavior_changes_cannot_opt_out_of_customer_notice(self):
        for impact in ("behavior", "breaking"):
            record = entry(developer_notice=developer_notice(publish=False, summary=""))
            record["api"]["impact"] = impact
            with self.assertRaisesRegex(ValueError, "require a developer notice"):
                rn.validate_entry(record)

    def test_compatible_fix_requiring_reruns_cannot_opt_out_of_customer_notice(self):
        for release in (False, True):
            for action in ("", "Reprocess affected jobs."):
                with self.subTest(release=release, action=action):
                    record = entry(customer_action="Reprocess affected jobs.",
                                   developer_notice=developer_notice(publish=False, summary="", action=action))
                    with self.assertRaisesRegex(ValueError, "require a developer notice"):
                        rn.validate_entry(record, release=release)

    def test_compatible_changes_without_required_action_can_opt_out(self):
        for action in ("none", "None.", "no action required", "No action required.", "  NONE.  "):
            with self.subTest(action=action):
                record = entry(customer_action=action,
                               developer_notice=developer_notice(publish=False, summary=""))
                rn.validate_entry(record)
                self.assertFalse(rn.customer_visible(record))

    def test_non_api_customer_action_does_not_require_a_developer_notice(self):
        record = entry(customer_action="Refresh the web page.",
                       api={"impact": "none", "assessment": "Only UI spacing changes, no API effect.", "endpoints": []},
                       developer_notice=developer_notice(publish=False, summary=""))
        rn.validate_entry(record)
        self.assertFalse(rn.customer_visible(record))

    def test_breaking_notice_keeps_migration_link_and_effective_date(self):
        record = entry(customer_action="Migrate before 1 November.", notice_date="2026-10-01", effective_date="2026-11-01",
                       notice_url="https://app.kanopy-ai.com/updates?release=2026.10.1",
                       developer_notice=developer_notice(action="Migrate before 1 November."))
        record["api"]["impact"] = "breaking"
        rn.validate_entry(record)
        change = rn.public_change(record)
        self.assertEqual(change["effective_date"], "2026-11-01")
        self.assertEqual(change["notice_url"], record["notice_url"])

    def test_customer_notice_is_bounded_and_publication_flag_is_boolean(self):
        for notice in (developer_notice(summary="x" * 281), developer_notice(action="x" * 481),
                       developer_notice(publish="false"), developer_notice(reason="")):
            with self.assertRaises(ValueError):
                rn.validate_entry(entry(developer_notice=notice))

    def test_upcoming_never_changes_released_history(self):
        old = {"releases": [{"version": "2026.10.1", "date": "2026-10-01", "changes": []}]}
        upcoming, released = rn.update_public(manifest(), {"releases": []}, old)
        self.assertEqual(released, old)
        self.assertEqual(upcoming["releases"][0]["timing"], "Release date not scheduled")
        self.assertNotIn("base_sha", json.dumps(upcoming))
        self.assertNotIn("repository", json.dumps(upcoming))

    def test_production_requires_proof_and_preserves_earlier_releases(self):
        with self.assertRaisesRegex(ValueError, "verified"):
            rn.update_public(manifest(state="deployed"), {"releases": []}, {"releases": []})
        record = manifest(state="deployed", deployment={"verified": True, "completed_at": "2026-10-06T15:00:00Z"})
        old = {"releases": [{"version": "2026.10.9", "date": "2026-10-05", "changes": []}]}
        up, rel = rn.update_public(record, {"releases": [rn.public_bundle(record)]}, old, today=date(2026, 10, 6))
        self.assertEqual(up["releases"], [])
        self.assertEqual(rel["releases"][0]["version"], "2026.10.10")
        self.assertEqual(rel["releases"][1], old["releases"][0])
        self.assertEqual(rn.update_public(record, up, rel, today=date(2026, 10, 7)), (up, rel))

    def test_internal_changes_not_in_customer_output(self):
        record = manifest(entries=[entry(type="internal")])
        up, rel = rn.update_public(record, {"releases": []}, {"releases": []})
        self.assertEqual(up["releases"], [])
        self.assertEqual(rel["releases"], [])
        self.assertIn("Restore evidence", rn.render_brief(record))

    def test_flags_and_dependencies_not_misrepresented_as_available(self):
        for record in (entry(availability="feature_flag", rollout="Not enabled; awaiting rollout."),
                       entry(availability="coordinated", dependencies=["Frontend deployment"], rollout="After frontend rollout."),
                       entry(dependencies=["SDK release"])):
            self.assertNotEqual(rn.public_change(record, deployed=True)["availability"], "Available")
        self.assertEqual(rn.public_change(entry(), deployed=True)["availability"], "Available")

    def test_repeated_candidate_updates_do_not_duplicate_upcoming(self):
        first = manifest()
        up, rel = rn.update_public(first, {"releases": []}, {"releases": []})
        second = manifest(head_sha="d" * 40, entries=[entry(summary="Newer scope.")])
        up, rel = rn.update_public(second, up, rel)
        self.assertEqual(len(up["releases"]), 1)
        self.assertEqual(up["releases"][0]["changes"][0]["body"], "Newer scope.")


class RepositoryTests(unittest.TestCase):
    def test_source_only_promotion_advances_coverage_without_hiding_undeployed_services(self):
        from datetime import datetime, timezone
        Path('source.py').write_text('after = True\n')
        self.save('source-only promotion')
        source_only = rn.commit('HEAD')
        started = int(rn.git('show', '-s', '--format=%ct', self.base).strip())
        run = {'id': 1, 'run_attempt': 1, 'head_sha': source_only,
               'run_started_at': datetime.fromtimestamp(started + 1, timezone.utc).isoformat(),
               'html_url': 'source-only-run', 'conclusion': 'success', 'status': 'completed',
               'head_branch': 'main', 'path': '.github/workflows/deploy.yml'}
        jobs = [{'name': name, 'conclusion': 'success'} for name in (
            'Verify promotion candidate', 'Verify promotion without service changes')]
        gh = rn.GitHub('KanopyAI/Powerline_3D', 'unused')
        runs = [run]
        with patch.object(rn, 'source_config', return_value={'repository': gh.repo, 'tracking_start_sha': self.base}), \
                patch.object(rn, 'fetch_sha'), patch.object(rn, 'successful_deployments', return_value=runs), \
                patch.object(gh, 'pages', return_value=jobs):
            self.assertEqual(rn.coverage_base(gh, source_only), (source_only, run['html_url']))
            service = Path('containers/powerline_analysis/analysis.py')
            service.parent.mkdir(parents=True)
            service.write_text('changed = True\n')
            self.save('service change without a successful deployment')
            Path('source.py').write_text('another_source_change = True\n')
            self.save('later source-only promotion')
            later = rn.commit('HEAD')
            runs.insert(0, {**run, 'id': 2, 'head_sha': later,
                           'run_started_at': datetime.fromtimestamp(started + 2, timezone.utc).isoformat()})
            self.assertEqual(rn.coverage_base(gh, later), (source_only, run['html_url']))


    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        os.chdir(self.temp.name)
        rn.git("init", "-q")
        rn.git("config", "user.name", "Test")
        rn.git("config", "user.email", "test@example.invalid")
        Path("source.py").write_text("before = True\n")
        self.save("base")
        self.base = rn.commit("HEAD")

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def save(self, message):
        rn.git("add", ".")
        rn.git("commit", "-qm", message)

    def add_entry(self):
        Path(rn.NOTES).mkdir(exist_ok=True)
        self.path = Path(rn.NOTES, "evidence-download-fix.json")
        self.path.write_text(json.dumps(entry(developer_notice=developer_notice(), source_digest=rn.source_digest(self.base, "HEAD"),
                                              source_files=rn.source_files(self.base, "HEAD"))))
        self.save("impact")

    def test_new_feature_requires_notice_but_historical_promotion_stays_valid(self):
        Path("source.py").write_text("after = True\n")
        self.save("feature")
        self.add_entry()
        record = json.loads(self.path.read_text())
        record.pop("developer_notice")
        self.path.write_text(json.dumps(record))
        self.save("legacy record")
        self.assertEqual(len(rn.read_entries(self.base, "HEAD")), 1)
        with self.assertRaisesRegex(ValueError, "New impact entries require developer_notice"):
            rn.read_entries(self.base, "HEAD", feature=True)

    def divergent_candidate(self):
        Path("source.py").write_text("first = 0\n" + "# gap\n" * 12 + "last = 0\n")
        self.save("shared source")
        self.base = rn.commit("HEAD")
        Path("source.py").write_text(Path("source.py").read_text().replace("first = 0", "first = 1"))
        self.save("staging hotfix")
        self.add_entry()
        staging = rn.commit("HEAD")
        rn.git("checkout", "-q", "-b", "feature", self.base)
        Path("source.py").write_text(Path("source.py").read_text().replace("last = 0", "last = 1"))
        self.save("dev feature")
        Path(rn.NOTES).mkdir(exist_ok=True)
        record = entry(id="dev-feature", developer_notice=developer_notice(),
                       source_digest=rn.source_digest(self.base, "HEAD"), source_files=rn.source_files(self.base, "HEAD"))
        Path(rn.NOTES, "dev-feature.json").write_text(json.dumps(record))
        self.save("dev impact")
        return staging, rn.commit("HEAD")

    def test_clean_merge_requires_combined_assessment_and_reports_exact_hashes(self):
        staging, dev = self.divergent_candidate()
        self.assertEqual(len(rn.read_entries(self.base, staging)), 1)
        self.assertEqual(len(rn.read_entries(self.base, dev)), 1)
        rn.git("merge", "--no-ff", "-m", "combine", staging)
        with self.assertRaisesRegex(ValueError, "candidate=.*recorded=.*") as failure:
            rn.read_entries(self.base, "HEAD")
        self.assertIn("Baseline: " + self.base, str(failure.exception))
        record = entry(id="reconcile-source", developer_notice=developer_notice(),
                       source_digest=rn.source_digest(dev, "HEAD"), source_files=rn.source_files(dev, "HEAD"))
        Path(rn.NOTES, "reconcile-source.json").write_text(json.dumps(record))
        self.save("review combined source")
        self.assertEqual(len(rn.read_entries(dev, "HEAD", feature=True, reconcile=staging)), 2)
        self.assertEqual(len(rn.read_entries(self.base, "HEAD")), 3)
        imported = Path(rn.NOTES, "evidence-download-fix.json")
        original = imported.read_text()
        imported.write_text(original.replace("Restore evidence downloads.", "Rewrite history."))
        self.save("tamper imported record")
        with self.assertRaisesRegex(ValueError, "stale"):
            rn.read_entries(dev, "HEAD", feature=True, reconcile=staging)

    def test_reconciliation_cannot_reuse_only_parent_assessments(self):
        staging, dev = self.divergent_candidate()
        with self.assertRaisesRegex(ValueError, "ancestor"):
            rn.read_entries(self.base, dev, feature=True, reconcile=staging)
        rn.git("merge", "--no-ff", "-m", "combine", staging)
        with self.assertRaisesRegex(ValueError, "new impact assessment"):
            rn.read_entries(dev, "HEAD", feature=True, reconcile=staging)

    def test_controller_preserves_reviewed_records_imported_from_staging(self):
        staging, dev = self.divergent_candidate()
        rn.git("merge", "--no-ff", "-m", "combine", staging)
        record = entry(id="reconcile-source", developer_notice=developer_notice(),
                       source_digest=rn.source_digest(dev, "HEAD"), source_files=rn.source_files(dev, "HEAD"))
        Path(rn.NOTES, "reconcile-source.json").write_text(json.dumps(record))
        self.save("review reconciliation")
        head = rn.commit("HEAD")
        gh = rn.GitHub("KanopyAI/kanopy-backend", "unused")
        pr = {"state": "open", "title": "Reconcile", "body": "", "base": {"ref": "dev", "sha": dev},
              "head": {"sha": head, "ref": "codex/reconcile", "repo": {"full_name": gh.repo}}}
        with patch.object(gh, "api", side_effect=[pr, {"commit": {"sha": staging}}]), \
                patch.object(controller, "draft_entry") as model, patch.object(controller, "write_commit") as write:
            controller.draft_pr(gh, 7)
            model.assert_not_called()
            write.assert_not_called()
        self.assertEqual(rn.git("status", "--porcelain"), "")

    def test_preflight_uses_combined_tree_and_cleans_up_on_failure(self):
        staging, dev = self.divergent_candidate()
        before = rn.git("worktree", "list", "--porcelain")
        with self.assertRaisesRegex(ValueError, "missing a current impact"):
            rn.preflight(dev, staging, base=self.base)
        self.assertEqual(rn.git("worktree", "list", "--porcelain"), before)
        self.assertEqual(rn.commit("HEAD"), dev)
        self.assertEqual(rn.git("status", "--porcelain"), "")

    def test_preflight_tests_execute_in_the_validated_tree(self):
        Path("source.py").write_text("combined = True\n")
        self.save("feature")
        self.add_entry()
        candidate = rn.commit("HEAD")
        rn.preflight(candidate, self.base, base=self.base, run=[sys.executable, "-c",
            "from pathlib import Path; import subprocess; "
            "assert Path('source.py').read_text() == 'combined = True\\n'; "
            "assert subprocess.check_output(['git', 'show', 'HEAD:source.py'], text=True) == Path('source.py').read_text()"])
        with self.assertRaises(subprocess.CalledProcessError):
            rn.preflight(candidate, self.base, base=self.base, run=[sys.executable, "-c", "raise SystemExit(2)"])
        self.assertEqual(rn.commit("HEAD"), candidate)

    def test_append_only_notice_correction_preserves_assessment_and_history(self):
        self.add_entry()
        original = json.loads(self.path.read_text())
        correction = dict(original, id="correct-download-notice", supersedes=original["id"],
                          developer_notice=developer_notice(summary="Corrected customer wording."))
        corrected_path = Path(rn.NOTES, correction["id"] + ".json")
        corrected_path.write_text(json.dumps(correction))
        self.save("correct notice")
        entries = rn.read_entries(self.base, "HEAD")
        preview = rn.public_bundle(manifest(entries=entries))
        self.assertEqual([c["body"] for c in preview["changes"]], ["Corrected customer wording."])
        published = {"releases": [{"source_id": "backend-pr-304", "changes": [{"body": "Already published."}]}]}
        _, history = rn.update_public(manifest(entries=entries), {"releases": []}, published)
        self.assertEqual(history, published)
        correction["api"] = dict(correction["api"], impact="none")
        corrected_path.write_text(json.dumps(correction))
        self.save("hide API impact")
        with self.assertRaisesRegex(ValueError, "preserve the original assessment"):
            rn.read_entries(self.base, "HEAD")

    def test_notice_correction_cannot_repeat_an_existing_base_correction(self):
        self.add_entry()
        original = json.loads(self.path.read_text())
        first = dict(original, id="first-notice-correction", supersedes=original["id"])
        Path(rn.NOTES, first["id"] + ".json").write_text(json.dumps(first))
        self.save("first correction")
        feature_base = rn.commit("HEAD")
        second = dict(first, id="second-notice-correction",
                      source_digest=rn.source_digest(feature_base, "HEAD"),
                      source_files=rn.source_files(feature_base, "HEAD"))
        Path(rn.NOTES, second["id"] + ".json").write_text(json.dumps(second))
        self.save("second correction")
        with self.assertRaisesRegex(ValueError, "Multiple notice corrections"):
            rn.read_entries(feature_base, "HEAD", feature=True)

    def test_promotion_rejects_correction_of_a_notice_already_in_production(self):
        self.add_entry()
        deployed = rn.commit("HEAD")
        original = json.loads(self.path.read_text())
        correction = dict(original, id="correct-published-notice", supersedes=original["id"],
                          source_digest=rn.source_digest(deployed, "HEAD"), source_files=[])
        Path(rn.NOTES, correction["id"] + ".json").write_text(json.dumps(correction))
        self.save("correct already deployed notice")
        with self.assertRaisesRegex(ValueError, "already in the release baseline"):
            rn.read_entries(deployed, "HEAD")
        with self.assertRaisesRegex(ValueError, "already in the release baseline"):
            rn.read_entries(deployed, "HEAD", release=True)

    def test_production_rollback_cannot_be_silently_skipped(self):
        from datetime import datetime, timezone
        Path('source.py').write_text('after = True\n')
        self.save('forward deployment')
        forward = rn.commit('HEAD')
        start = int(rn.git('show', '-s', '--format=%ct', self.base).strip())
        timestamp = lambda offset: datetime.fromtimestamp(start + offset, timezone.utc).isoformat()
        runs = [{'id': 2, 'head_sha': self.base, 'run_started_at': timestamp(2), 'html_url': 'rollback'},
                {'id': 1, 'head_sha': forward, 'run_started_at': timestamp(1), 'html_url': 'forward'}]
        gh = rn.GitHub('KanopyAI/kanopy-backend', 'unused')
        with patch.object(rn, 'source_config', return_value={'repository': gh.repo, 'tracking_start_sha': self.base}), \
                patch.object(rn, 'fetch_sha'), patch.object(rn, 'successful_deployments', return_value=runs), \
                patch.object(rn, 'verify_deployment', return_value=True):
            with self.assertRaisesRegex(ValueError, 'rollback'):
                rn.coverage_base(gh, forward)

    def test_ios_promotions_start_after_the_last_verified_release(self):
        Path('source.py').write_text('released = True\n')
        self.save('released build')
        released = rn.commit('HEAD')
        Path('source.py').write_text('next = True\n')
        self.save('next candidate')
        head = rn.commit('HEAD')
        gh = rn.GitHub('KanopyAI/Kanopy-ios-app', 'unused')
        with patch.object(rn, 'source_config', return_value={'repository': gh.repo, 'tracking_start_sha': self.base}), \
                patch.object(rn, 'fetch_sha'):
            self.assertEqual(rn.coverage_base(gh, head), (self.base, ''))
            self.assertEqual(rn.coverage_base(gh, head, verified=(released, 'https://github.com/run/1')),
                             (released, 'https://github.com/run/1'))
            with self.assertRaisesRegex(ValueError, 'rollback'):
                rn.coverage_base(gh, released, verified=(head, 'https://github.com/run/2'))

    def test_missing_note_then_real_record_then_stale_record(self):
        Path("source.py").write_text("after = True\n")
        self.save("feature")
        with self.assertRaisesRegex(ValueError, "needs a customer-impact"):
            rn.read_entries(self.base, "HEAD", feature=True)
        self.add_entry()
        self.assertEqual(len(rn.read_entries(self.base, "HEAD", feature=True)), 1)
        Path("source.py").write_text("after = False\n")
        self.save("changed scope")
        with self.assertRaisesRegex(ValueError, "stale"):
            rn.read_entries(self.base, "HEAD", feature=True)

    def test_released_records_cannot_be_modified_or_removed(self):
        self.add_entry()
        base_with_note = rn.commit("HEAD")
        self.path.write_text(json.dumps(entry(summary="Silently rewrite history.")))
        self.save("rewrite")
        with self.assertRaisesRegex(ValueError, "existing release"):
            rn.read_entries(base_with_note, "HEAD")
        self.path.unlink()
        self.save("delete")
        with self.assertRaisesRegex(ValueError, "existing release"):
            rn.read_entries(base_with_note, "HEAD")

    def test_symlink_note_rejected(self):
        Path(rn.NOTES).mkdir()
        Path(rn.NOTES, "evidence-download-fix.json").symlink_to("../source.py")
        self.save("symlink")
        with self.assertRaisesRegex(ValueError, "ordinary"):
            rn.read_entries(self.base, "HEAD")

    def test_promotion_cannot_hide_another_unassessed_feature(self):
        Path("source.py").write_text("feature = 1\n")
        self.save("feature")
        self.add_entry()
        self.assertEqual(len(rn.read_entries(self.base, "HEAD")), 1)
        Path("source.py").write_text("feature = 2\n")
        self.save("another feature")
        with self.assertRaisesRegex(ValueError, "missing a current impact"):
            rn.read_entries(self.base, "HEAD")

    def test_digest_is_independent_of_local_hash_abbreviation_settings(self):
        Path("source.py").write_text("feature = 1\n")
        self.save("feature")
        before = rn.source_digest(self.base, "HEAD")
        rn.git("config", "core.abbrev", "12")
        self.assertEqual(rn.source_digest(self.base, "HEAD"), before)


class DeploymentTests(unittest.TestCase):
    def test_current_run_is_not_its_own_baseline(self):
        previous = {"id": 1, "head_sha": "a" * 40, "run_started_at": "2026-10-01T00:00:00Z"}
        current = {"id": 2, "head_sha": "b" * 40, "run_started_at": "2026-10-06T00:00:00Z"}
        self.assertEqual(rn.deployment_baseline([current, previous], current), previous)
        with self.assertRaises(ValueError):
            rn.deployment_baseline([current], current)

    def test_failed_and_wrong_commit_deployments_never_mark_released(self):
        pr = {"base": {"ref": "main"}, "head": {"ref": "staging", "repo": {"full_name": "KanopyAI/kanopy-backend"}},
              "merged": True, "merge_commit_sha": "a" * 40}
        gh = rn.GitHub("KanopyAI/kanopy-backend", "unused")
        run = {"conclusion": "failure", "status": "completed", "head_branch": "main",
               "head_sha": "a" * 40, "path": ".github/workflows/deploy.yml"}
        with self.assertRaisesRegex(ValueError, "does not prove"):
            controller.make_manifest(gh, pr, run=run)
        run.update(conclusion="success", head_sha="b" * 40)
        with self.assertRaisesRegex(ValueError, "does not prove"):
            controller.make_manifest(gh, pr, run=run)

    def test_hotfix_to_main_is_drafted_and_only_released_by_a_verified_deployment(self):
        gh = rn.GitHub("KanopyAI/kanopy-backend", "unused")
        event = {"pull_request": {"number": 7, "base": {"ref": "main"}, "head": {"ref": "hotfix/urgent"}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "event.json")
            path.write_text(json.dumps(event))
            environment = {"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": gh.repo, "GH_TOKEN": "x"}
            with patch.dict(os.environ, environment), patch("sys.argv", ["controller", "event", "--dry-run"]), \
                    patch.object(controller, "draft_pr") as draft, patch.object(controller, "sync") as sync:
                controller.main()
                draft.assert_called_once()
                sync.assert_not_called()
            event["pull_request"]["head"]["ref"] = "staging"
            path.write_text(json.dumps(event))
            with patch.dict(os.environ, environment), patch("sys.argv", ["controller", "event", "--dry-run"]), \
                    patch.object(controller, "draft_pr") as draft, patch.object(controller, "sync") as sync:
                controller.main()
                sync.assert_called_once()
                draft.assert_not_called()
        pr = {"state": "open", "number": 7, "html_url": "u", "title": "Hotfix", "body": "", "base": {"ref": "main", "sha": "b" * 40},
              "head": {"ref": "hotfix/urgent", "sha": "a" * 40, "repo": {"full_name": gh.repo}},
              "merged": True, "merge_commit_sha": "a" * 40}
        with patch.object(gh, "api", return_value=pr), patch.object(rn, "fetch_sha"), \
                patch.object(rn, "git", return_value="b" * 40), patch.object(rn, "changed_paths", return_value=["app/x.py"]), \
                patch.object(rn, "source_diff", return_value="diff") as diff, patch.object(rn, "source_digest", return_value="a" * 64):
            controller.draft_pr(gh, 7, dry_run=True)
            diff.assert_called_once()
        with self.assertRaisesRegex(ValueError, "promotion"):
            controller.make_manifest(gh, pr)
        run = {"id": 2, "head_sha": "a" * 40, "conclusion": "success", "status": "completed", "head_branch": "main",
               "path": ".github/workflows/deploy.yml", "run_started_at": "2026-10-06", "html_url": "run", "updated_at": "t"}
        with patch.object(rn, "verify_deployment", return_value=True), \
                patch.object(rn, "coverage_base", return_value=("b" * 40, "previous-run")), \
                patch.object(rn, "changed_paths", return_value=[]), patch.object(rn, "read_entries", return_value=[entry()]):
            self.assertEqual(controller.make_manifest(gh, pr, run=run)["state"], "deployed")

    def test_forks_and_promotions_cannot_trigger_model_drafting(self):
        for base, branch, repo in [("main", "staging", "KanopyAI/kanopy-backend"),
                                   ("dev", "feature", "outside/fork"),
                                   ("staging", "dev", "KanopyAI/kanopy-backend")]:
            gh = rn.GitHub("KanopyAI/kanopy-backend", "unused")
            with patch.object(gh, "api", return_value={"state": "open", "base": {"ref": base},
                "head": {"ref": branch, "repo": {"full_name": repo}}}), patch.object(controller, "draft_entry") as model:
                controller.draft_pr(gh, 5)
                model.assert_not_called()


class ControllerSafetyTests(unittest.TestCase):
    def test_source_push_during_model_call_does_not_overwrite_code(self):
        pr = {"state": "open", "title": "Fix evidence", "body": "Regression fix",
              "head": {"sha": "a" * 40, "ref": "fix/evidence", "repo": {"full_name": "KanopyAI/kanopy-backend"}},
              "base": {"sha": "b" * 40, "ref": "dev"}}
        moved = json.loads(json.dumps(pr))
        moved["head"]["sha"] = "c" * 40
        gh = rn.GitHub("KanopyAI/kanopy-backend", "unused")
        with patch.object(gh, "api", side_effect=[pr, {"commit": {"sha": "b" * 40}}, moved]), patch.object(rn, "fetch_sha"), \
                patch.object(rn, "git", return_value="b" * 40), \
                patch.object(rn, "changed_paths", return_value=["app/api/routes/evidence.py"]), \
                patch.object(rn, "source_diff", return_value="safe diff"), \
                patch.object(rn, "source_digest", return_value="a" * 64), \
                patch.object(rn, "source_files", return_value=[]), \
                patch.object(controller, "draft_entry", return_value=entry(developer_notice=developer_notice())), \
                patch.object(controller, "write_commit") as write, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "RELEASE_NOTES_TOKEN": "fake"}):
            with self.assertRaisesRegex(ValueError, "changed during drafting"):
                controller.draft_pr(gh, 1)
            write.assert_not_called()

    def test_repeated_draft_event_is_a_noop_and_keeps_review_edits(self):
        target = rn.GitHub(controller.TARGET_REPO, "unused")
        release = manifest()
        def file(path, ref):
            return {".github/release-notes-target.json": {"schema_version": 1, "sources": ["KanopyAI/kanopy-backend"]},
                    "public/upcoming.json": {"releases": []}, "public/releases.json": {"releases": []},
                    "release-drafts/pending.json": {release["id"]: release}}[path]
        with patch.object(target, "file", side_effect=file), \
                patch.object(target, "api", return_value={"object": {"sha": "a" * 40}}), \
                patch.object(target, "pages", side_effect=[
                    [{"state": "open", "number": 8, "html_url": "https://github.com/example/pull/8"}],
                    [{"ref": "refs/heads/automation/release-notes", "object": {"sha": "b" * 40}}]]), \
                patch.object(controller, "write_commit") as write:
            controller.publish_draft(target, release)
            write.assert_not_called()

    def test_closed_draft_is_not_reopened_after_an_earlier_publication(self):
        target = rn.GitHub(controller.TARGET_REPO, "unused")
        with patch.object(target, "file", side_effect=[
                    {"schema_version": 1, "sources": ["KanopyAI/kanopy-backend"]}, {"releases": []}, {"releases": []}]), \
                patch.object(target, "api", return_value={"object": {"sha": "a" * 40}}), \
                patch.object(target, "pages", return_value=[
                    {"state": "closed", "number": 9, "merged_at": None},
                    {"state": "closed", "number": 8, "merged_at": "2026-10-05"}]), \
                patch.object(controller, "write_commit") as write:
            with self.assertRaisesRegex(ValueError, "closed without merging"):
                controller.publish_draft(target, manifest())
            write.assert_not_called()

    def test_same_commit_redeploy_does_not_create_an_empty_replacement_release(self):
        gh = rn.GitHub("KanopyAI/kanopy-backend", "unused")
        pr = {"base": {"ref": "main"}, "head": {"ref": "staging", "repo": {"full_name": gh.repo}},
              "merged": True, "merge_commit_sha": "a" * 40}
        run = {"id": 2, "head_sha": "a" * 40, "conclusion": "success", "status": "completed",
               "head_branch": "main", "path": ".github/workflows/deploy.yml", "run_started_at": "2026-10-06"}
        with patch.object(rn, "verify_deployment", return_value=True), \
                patch.object(rn, "coverage_base", return_value=("a" * 40, "previous-run")):
            self.assertIsNone(controller.make_manifest(gh, pr, run=run))


class DraftWordingTests(unittest.TestCase):
    def setUp(self):
        self.target = rn.GitHub(controller.TARGET_REPO, "unused")
        self.main_sha, self.branch_sha = "a" * 40, "b" * 40
        self.previous = manifest()
        self.incoming = manifest(id="powerline-pr-10", repository="KanopyAI/Powerline_3D")
        self.prs = [{"state": "closed", "number": 8, "merged_at": "2026-10-06",
                     "head": {"sha": self.branch_sha}}]
        self.branches = [{"ref": "refs/heads/automation/release-notes",
                          "object": {"sha": self.branch_sha}}]
        branch_upcoming = rn.public_bundle(self.previous)
        branch_upcoming.update(title="Wording from merged draft", timing="Older timing")
        main_upcoming = rn.public_bundle(self.previous)
        main_upcoming.update(title="Corrected title on main", timing="Corrected timing on main")
        main_upcoming["changes"][0]["body"] = "Corrected explanation on main."
        self.main_files = {
            "release-drafts/pending.json": {self.previous["id"]: self.previous},
            "public/upcoming.json": {"releases": [main_upcoming]},
            "public/releases.json": {"releases": []},
        }
        self.branch_files = {
            **self.main_files,
            "public/upcoming.json": {"releases": [branch_upcoming]},
        }

    def publish(self, incoming=None):
        def file(path, ref):
            snapshot = self.main_files if ref == self.main_sha else self.branch_files
            return json.loads(json.dumps(snapshot[path]))

        with patch.object(self.target, "file", side_effect=file), \
                patch.object(self.target, "api", return_value={
                    "object": {"sha": self.main_sha}, "html_url": "https://github.com/example/pull/9"}), \
                patch.object(self.target, "pages", side_effect=[self.prs, self.branches]), \
                patch.object(controller, "write_commit", return_value="c" * 40) as write:
            controller.publish_attempt(self.target, incoming or self.incoming)
        if not write.called:
            return None
        self.assertEqual(write.call_args.kwargs["base_sha"], self.main_sha)
        self.assertEqual(write.call_args.kwargs["parents"],
                         [self.branch_sha, self.main_sha] if self.branches else [self.main_sha])
        return {path: json.loads(content) for path, content in write.call_args.kwargs["files"].items()
                if path.endswith(".json")}

    def assert_wording(self, files, snapshot):
        previous = next(r for r in files["public/upcoming.json"]["releases"]
                        if r["id"] == self.previous["id"])
        self.assertEqual(previous, snapshot["public/upcoming.json"]["releases"][0])
        self.assertEqual({r["id"] for r in files["public/upcoming.json"]["releases"]},
                         {self.previous["id"], self.incoming["id"]})

    def test_merged_branch_cannot_replace_later_main_corrections(self):
        self.assert_wording(self.publish(), self.main_files)

    def test_main_corrections_survive_when_merged_branch_is_deleted(self):
        self.branches = []
        self.assert_wording(self.publish(), self.main_files)

    def test_open_draft_keeps_its_review_edits(self):
        self.prs.append({"state": "open", "number": 9,
                         "html_url": "https://github.com/example/pull/9"})
        self.assert_wording(self.publish(), self.branch_files)

    def test_first_draft_recovers_review_edits_after_branch_write(self):
        self.prs = []
        self.assert_wording(self.publish(), self.branch_files)

    def test_later_draft_recovers_after_branch_write_before_pr_creation(self):
        self.prs[0]["head"]["sha"] = "d" * 40
        self.branch_files["release-drafts/pending.json"] = {
            self.previous["id"]: self.previous, self.incoming["id"]: self.incoming}
        self.assert_wording(self.publish(), self.branch_files)

    def test_repeated_event_after_merge_does_not_reopen_stale_draft(self):
        self.assertIsNone(self.publish(self.previous))

    def test_incoming_release_still_updates_its_own_wording(self):
        updated = manifest(entries=[entry(summary="Updated source scope.")])
        files = self.publish(updated)
        self.assertEqual(files["public/upcoming.json"]["releases"][0]["changes"][0]["body"],
                         "Updated source scope.")


class MultiComponentTests(unittest.TestCase):
    def test_source_only_powerline_promotion_moves_upcoming_to_released(self):
        gh = rn.GitHub('KanopyAI/Powerline_3D', 'unused')
        pr = {'number': 42, 'html_url': f'https://github.com/{gh.repo}/pull/42',
              'base': {'ref': 'main'}, 'head': {'ref': 'staging', 'sha': 'b' * 40,
                                             'repo': {'full_name': gh.repo}},
              'merged': True, 'merge_commit_sha': 'c' * 40}
        run = {'id': 1, 'run_attempt': 1, 'head_sha': pr['merge_commit_sha'],
               'conclusion': 'success', 'status': 'completed', 'head_branch': 'main',
               'path': '.github/workflows/deploy.yml', 'html_url': 'verified-source-only-run',
               'updated_at': '2026-10-07T12:00:00Z'}
        jobs = [{'name': name, 'conclusion': 'success'} for name in (
            'Verify promotion candidate', 'Verify promotion without service changes')]
        with patch.object(rn, 'coverage_base', return_value=('a' * 40, 'previous-run')), \
                patch.object(rn, 'changed_paths', return_value=['docs/customer-workflow.md']), \
                patch.object(rn, 'read_entries', return_value=[entry()]) as read, \
                patch.object(gh, 'pages', return_value=jobs):
            upcoming = controller.make_manifest(gh, pr)
            deployed = controller.make_manifest(gh, pr, run=run)
            read.assert_called_with('a' * 40, run['head_sha'], release=True)
        pending, up, releases = controller.combine_pending(
            deployed, {upcoming['id']: upcoming}, {'releases': [rn.public_bundle(upcoming)]}, {'releases': []})
        self.assertEqual(pending[upcoming['id']]['state'], 'deployed')
        self.assertEqual(deployed['deployment']['url'], run['html_url'])
        self.assertEqual(up['releases'], [])
        self.assertEqual(releases['releases'][0]['source_id'], upcoming['id'])
        self.assertEqual(releases['releases'][0]['deployed_at'], run['updated_at'])
        self.assertIsNone(controller.combine_pending(deployed, pending, up, releases))


    def test_powerline_no_service_check_requires_success_and_no_outstanding_services(self):
        gh = rn.GitHub('KanopyAI/Powerline_3D', 'unused')
        run = {'id': 1, 'run_attempt': 1, 'conclusion': 'success', 'status': 'completed',
               'head_branch': 'main', 'path': '.github/workflows/deploy.yml'}
        jobs = [{'name': 'Verify promotion candidate', 'conclusion': 'success'},
                {'name': 'Verify promotion without service changes', 'conclusion': 'success'}]
        with patch.object(gh, 'pages', return_value=jobs):
            for services in (None, {'analysis'}, {'clustering', 'reconstruct'}):
                with self.subTest(services=services):
                    self.assertFalse(rn.verify_deployment(gh, run, required_services=services))
            for job in jobs:
                for conclusion in ('failure', 'cancelled', 'skipped', None):
                    with self.subTest(job=job['name'], conclusion=conclusion):
                        job['conclusion'] = conclusion
                        self.assertFalse(rn.verify_deployment(gh, run, required_services=set()))
                job['conclusion'] = 'success'
            for overrides in ({'conclusion': 'failure'}, {'status': 'in_progress'},
                              {'head_branch': 'staging'}, {'path': '.github/workflows/tests.yml'}):
                with self.subTest(overrides=overrides):
                    self.assertFalse(rn.verify_deployment(gh, {**run, **overrides}, required_services=set()))


    def test_other_component_events_preserve_reviewed_wording_and_publication(self):
        previous = {'releases': [{'source_id': 'frontend-pr-1', 'title': 'Reviewed wording'},
                                 {'source_id': 'backend-pr-2', 'title': 'Stale branch copy'}]}
        current = {'releases': [{'source_id': 'frontend-pr-1', 'title': 'Generated wording', 'version': '2026.10.2'},
                                {'source_id': 'backend-pr-2', 'title': 'Published wording'}]}
        controller.preserve_reviewed_wording(current, previous, key='source_id', incoming='ios-run-3',
                                            published={'backend-pr-2'})
        self.assertEqual(current['releases'][0]['title'], 'Reviewed wording')
        self.assertEqual(current['releases'][0]['version'], '2026.10.2')
        self.assertEqual(current['releases'][1]['title'], 'Published wording')

    def test_vendored_engine_matches_its_recorded_hashes(self):
        import hashlib
        directory = Path(__file__).resolve().parent
        metadata = directory.parent / 'release-notes-engine.json'
        if metadata.exists():
            for filename, expected in json.loads(metadata.read_text())['files'].items():
                self.assertEqual(hashlib.sha256((directory / filename).read_bytes()).hexdigest(), expected)

    def test_all_components_keep_their_own_customer_information(self):
        pending, up, rel = {}, {"releases": []}, {"releases": []}
        for repo, (component, _) in rn.SOURCES.items():
            record = manifest(id=f"{component}-pr-1", repository=repo)
            pending, up, rel = controller.combine_pending(record, pending, up, rel)
        self.assertEqual(len(pending), 4)
        self.assertEqual({r['id'] for r in up['releases']}, set(pending))
        self.assertEqual(rel['releases'], [])

    def test_late_event_cannot_regress_published_or_pending_deployment(self):
        ready = manifest(state="deployed", deployment={"verified": True, "completed_at": "2026-10-06T15:00:00Z"})
        pending, up, rel = controller.combine_pending(ready, {}, {"releases": []}, {"releases": []})
        self.assertIsNone(controller.combine_pending(manifest(), pending, up, rel))
        self.assertEqual(rn.update_public(manifest(), up, rel), (up, rel))

    def test_ios_upload_stays_upcoming(self):
        record = manifest(id="ios-run-10", repository="KanopyAI/Kanopy-ios-app", state="testflight")
        up, rel = rn.update_public(record, {"releases": []}, {"releases": []})
        self.assertIn("App Store release not confirmed", up['releases'][0]['timing'])
        self.assertEqual(rel['releases'], [])

    def test_powerline_partial_service_deployment_is_not_a_whole_release(self):
        gh = rn.GitHub("KanopyAI/Powerline_3D", "unused")
        run = {"id": 1, "run_attempt": 1, "conclusion": "success", "status": "completed",
               "head_branch": "main", "path": ".github/workflows/deploy.yml"}
        jobs = [{"name": "Refresh pipeline pod → prod", "conclusion": "success"},
                {"name": "Promote & deploy → prod (powerline_analysis, analysis, analysis)", "conclusion": "success"}]
        with patch.object(gh, "pages", return_value=jobs):
            self.assertFalse(rn.verify_deployment(gh, run))
            self.assertTrue(rn.verify_deployment(gh, run, required_services={'analysis'}))
            self.assertFalse(rn.verify_deployment(gh, run, required_services={'analysis', 'clustering'}))
            for service in ('orchestrator', 'data-prep', 'reconstruct', 'segmentation', 'clustering'):
                jobs.append({"name": f"Promote & deploy → prod ({service})", "conclusion": "success"})
            self.assertTrue(rn.verify_deployment(gh, run))
            jobs[-1]['conclusion'] = 'skipped'
            self.assertFalse(rn.verify_deployment(gh, run))

    def test_processing_dependencies_include_shared_and_base_consumers(self):
        self.assertEqual(rn.processing_services(['containers/Dockerfile.base.py311']), {'reconstruct', 'clustering'})
        self.assertEqual(rn.processing_services(['containers/shared/outputs.py']), set(rn.PROCESSING_SERVICES.values()))

    def test_new_ios_candidate_replaces_covered_upcoming_build(self):
        old = manifest(id='ios-run-1', repository='KanopyAI/Kanopy-ios-app', state='testflight')
        new = manifest(id='ios-run-2', repository=old['repository'], state='testflight', supersedes=[old['id']])
        pending, up, _ = controller.combine_pending(new, {old['id']: old}, {'releases': [rn.public_bundle(old)]}, {'releases': []})
        self.assertEqual(set(pending), {new['id']})
        self.assertEqual([r['id'] for r in up['releases']], [new['id']])

    def test_replaced_candidate_does_not_return_when_its_event_is_replayed(self):
        old = manifest(id='ios-run-1', repository='KanopyAI/Kanopy-ios-app', state='testflight')
        new = manifest(id='ios-run-2', repository=old['repository'], state='testflight', supersedes=[old['id']])
        pending, up, rel = controller.combine_pending(new, {old['id']: old}, {'releases': [rn.public_bundle(old)]}, {'releases': []})
        self.assertIsNone(controller.combine_pending(old, pending, up, rel))
        self.assertIsNone(controller.combine_pending(new, pending, up, rel))
        # State written before this guard may already hold both; replaying the newer build repairs it.
        stale = dict(pending, **{old['id']: old})
        stale_up = {'releases': up['releases'] + [rn.public_bundle(old)]}
        repaired, repaired_up, _ = controller.combine_pending(new, stale, stale_up, rel)
        self.assertEqual(set(repaired), {new['id']})
        self.assertEqual([r['id'] for r in repaired_up['releases']], [new['id']])

    def test_wording_merged_to_main_survives_a_rebuild_after_branch_deletion(self):
        target = rn.GitHub(controller.TARGET_REPO, "unused")
        other = manifest(id="frontend-pr-1", repository="KanopyAI/kanopy-frontend")
        reviewed = rn.public_bundle(other)
        reviewed["title"] = "Reviewed wording"
        files = {"release-drafts/pending.json": {other["id"]: other},
                 "public/upcoming.json": {"releases": [reviewed]}, "public/releases.json": {"releases": []}}
        with patch.object(target, "file", side_effect=lambda path, ref: json.loads(json.dumps(files[path]))), \
                patch.object(target, "api", return_value={"object": {"sha": "a" * 40}}), \
                patch.object(target, "pages", side_effect=[[], []]), \
                patch.object(controller, "write_commit", return_value=None) as write:
            controller.publish_attempt(target, manifest())
        upcoming = json.loads(write.call_args.kwargs["files"]["public/upcoming.json"])
        self.assertEqual({r["id"]: r["title"] for r in upcoming["releases"]},
                         {"frontend-pr-1": "Reviewed wording", "backend-pr-304": "Upcoming API and platform changes"})

    def test_ios_verified_release_comes_from_published_history(self):
        gh = rn.GitHub('KanopyAI/Kanopy-ios-app', 'unused')
        target = rn.GitHub(controller.TARGET_REPO, 'unused')
        files = {'public/releases.json': {'releases': [
                     {'source_id': 'ios-run-1', 'deployed_at': '2026-10-01T10:00:00Z'},
                     {'source_id': 'ios-run-2', 'deployed_at': '2026-10-05T10:00:00Z'},
                     {'source_id': 'backend-pr-9', 'deployed_at': '2026-10-06T10:00:00Z'}]},
                 'release-drafts/ios-run-2.json': {'head_sha': 'f' * 40, 'promotion_pr': 'https://github.com/run/2'}}
        with patch.object(target, 'file', side_effect=lambda path, ref: files[path]):
            self.assertEqual(controller.ios_verified_release(gh, target), ('f' * 40, 'https://github.com/run/2'))
            self.assertIsNone(controller.ios_verified_release(rn.GitHub('KanopyAI/kanopy-backend', 'unused'), target))
        self.assertIsNone(controller.ios_verified_release(gh, None))

    def test_previews_for_several_promotions_do_not_overwrite_each_other(self):
        gh = rn.GitHub("KanopyAI/kanopy-backend", "unused")
        with tempfile.TemporaryDirectory() as output, patch.object(gh, "api", return_value={"state": "open"}), \
                patch.object(controller, "make_manifest", side_effect=[manifest(id="backend-pr-1"), manifest(id="backend-pr-2")]), \
                patch.dict(os.environ, {"RELEASE_NOTES_TOKEN": ""}):
            controller.sync(gh, 1, output=output, dry_run=True)
            controller.sync(gh, 2, output=output, dry_run=True)
            self.assertEqual(sorted(p.name for p in Path(output).iterdir()), ["backend-pr-1", "backend-pr-2"])
            self.assertTrue(Path(output, "backend-pr-1", "customer-preview.json").exists())

    def test_writer_retries_races_and_refuses_other_failures(self):
        target = rn.GitHub(controller.TARGET_REPO, "unused")
        with patch.object(target, 'file', return_value={'schema_version': 1, 'sources': list(rn.SOURCES)}), \
                patch.object(controller, 'publish_attempt', side_effect=[RuntimeError('HTTP 422'), 'done']) as publish, \
                patch.object(controller.time, 'sleep'):
            self.assertEqual(controller.publish_draft(target, manifest()), 'done')
            self.assertEqual(publish.call_count, 2)
        with patch.object(target, 'file', return_value={'schema_version': 1, 'sources': list(rn.SOURCES)}), \
                patch.object(controller, 'publish_attempt', side_effect=RuntimeError('HTTP 403')) as publish:
            with self.assertRaises(RuntimeError):
                controller.publish_draft(target, manifest())
            self.assertEqual(publish.call_count, 1)


if __name__ == "__main__":
    unittest.main()
