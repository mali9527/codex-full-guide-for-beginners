"""Meaningful failure tests for the local writing review gate; no network calls."""
from datetime import date, timedelta
import importlib.util
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

SOURCE = Path(__file__).resolve().parents[1] / "writing_gate.py"
spec = importlib.util.spec_from_file_location("writing_gate", SOURCE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class WritingGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="writing-gate-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day = date(2026, 9, 26)
        (self.root / "tools").mkdir()
        shutil.copyfile(SOURCE, self.root / "tools/writing_gate.py")
        shutil.copyfile(SOURCE.parent / "official-sources.yaml", self.root / gate.AUTHORITY_PATH)
        (self.root / "manuscript").mkdir()
        (self.root / "checks/writing").mkdir(parents=True)
        for name in gate.RULE_PATHS:
            (self.root / name).write_text("# 本书规则\n具备日常电脑经验，不具备编程经验。\n", encoding="utf-8")
        self.book = {
            "id": "test-book", "product": "codex", "language": "zh-CN",
            "audience": "有电脑经验，无编程经验", "platforms": ["windows", "mac"],
            "baseline": {"checked_on": "2026-09-26", "product_version": "documented, not GUI tested"},
            "required_checks": ["editorial", "facts", "operations"],
            "units": [
                {"id": "base", "path": "manuscript/base.md", "facts": [], "prerequisites": []},
                {"id": "chapter", "path": "manuscript/chapter.md", "facts": ["local-project"], "prerequisites": ["base"]},
                {"id": "other", "path": "manuscript/other.md", "facts": [], "prerequisites": []},
            ],
        }
        self.facts = [{
            "id": "local-project", "claim": "可连接本地目录", "status": "confirmed",
            "scope": {"product": "codex"}, "checked_on": "2026-09-26",
            "sources": [{"url": "https://learn.chatgpt.com/docs/projects", "checked_on": "2026-09-26"}],
        }]
        self.save("book.yaml", self.book)
        self.save("facts.yaml", self.facts)
        (self.root / "manuscript/base.md").write_text("# 基础\n说明本书用途。\n", encoding="utf-8")
        (self.root / "manuscript/chapter.md").write_text("# 本章\n把文件夹连接到 Codex。\n这里讲当前任务。\n", encoding="utf-8")
        (self.root / "manuscript/other.md").write_text("# 另一章\n说明作者的教学安排。\n", encoding="utf-8")
        self.migration_patch = patch.dict(gate.MIGRATION_SNAPSHOTS, {
            uid: gate._digest(self.root / self.book["units"][index]["path"])
            for index, uid in enumerate(("base", "chapter", "other"))
        })
        self.migration_patch.start()
        self.addCleanup(self.migration_patch.stop)
        for uid in ("base", "chapter", "other"):
            self.save_review(uid, self.receipt(uid))

    def save(self, relative, value):
        (self.root / relative).write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def receipt(self, uid="chapter"):
        result = gate.prepare(self.root, uid, today=self.day, mode="retrospective")
        result["study_brief"].update(
            reader_goal="从已有办公经验理解本章任务和判断结果。",
            reader_start=["会管理文件，但不默认懂项目和权限。"],
            explain_now=["当前任务所需的陌生概念。"],
            teaching_sequence=["从具体问题出发，再讲必要概念与动作，然后核对结果。"],
            defer=["后续开发细节不提前铺陈。"], case_plan="使用自创材料，并区分请求与产品保证。",
        )
        if uid == "chapter":
            result["study_brief"]["official_readings"] = [{
                "fact_id": self.facts[0]["id"], "url": self.facts[0]["sources"][0]["url"],
                "locator": "Local projects", "checked_on": self.day.isoformat(),
                "understanding": "理解目录与本章工作范围的关系，并保留适用条件。",
                "teaching_use": "先让读者辨认具体材料，再介绍处理它们的入口。",
            }]
        result["study_review"] = {"result": "pass", "reason": "核对提要与本章讲法；这是对既有样章的回顾性校准。"}
        for item in result["reader_review"].values():
            item.update(result="pass", reason="逐段复核：只解释任务必需且读者未掌握的内容。")
        result["fact_review"].update(
            coverage="reviewed", reason="从完整正文核对所有产品断言；作者建议和案例数据单独识别。",
            docs_checked_on=self.day.isoformat(),
            change_review={"url": "https://learn.chatgpt.com/docs/changelog", "locator": "本次相关更新条目", "checked_on": self.day.isoformat(), "effect": "已比较相关变更；本条断言适用范围不变。"},
        )
        if uid == "chapter":
            result["fact_review"]["claims"] = [{
                "anchor": "把文件夹连接到 Codex。", "fact_id": "local-project",
                "url": self.facts[0]["sources"][0]["url"], "locator": "Local projects / connecting a folder",
                "checked_on": self.day.isoformat(), "surface": "codex-desktop",
                "platforms": ["windows", "mac"], "availability": "stable",
                "conditions": "桌面本地项目；文档依据，不表示实测。", "support": "supported",
                "rationale": "原页说明本地项目与目录关联；本句没有承诺默认状态或额外权限。", "notice": "",
            }]
        return result

    def save_review(self, uid, value):
        self.save("checks/writing/" + uid + ".yaml", value)

    def refresh(self, record):
        record["input_sha256"] = gate.fingerprint(self.root, record["unit"], record.get("evidence_paths", []))
        return record

    def check(self, publication=False, units=None):
        return gate.validate(self.root, units, publication, self.day)

    def codes(self, result, level="errors"):
        return {item["code"] for item in result[level]}

    def assert_quality_block(self, code):
        draft = self.check(units=["chapter"])
        self.assertEqual([], draft["errors"], draft)
        self.assertIn(code, self.codes(draft, "warnings"))
        self.assertIn(code, self.codes(self.check(True, ["chapter"])))

    def test_complete_records_are_accepted_without_claiming_gui_tests(self):
        result = self.check(True)
        self.assertEqual([], result["errors"])
        self.assertTrue(all(state == {"editorial": "pass", "facts": "pass"} for state in result["quality"].values()))

    def test_missing_record_allows_draft_but_blocks_publication(self):
        (self.root / "checks/writing/chapter.yaml").unlink()
        self.assert_quality_block("missing_writing_review")

    def test_prepare_is_pending_and_does_not_write_a_receipt(self):
        before = (self.root / "checks/writing/chapter.yaml").read_bytes()
        result = gate.prepare(self.root, "chapter", today=self.day)
        self.assertEqual(before, (self.root / "checks/writing/chapter.yaml").read_bytes())
        self.assertTrue(all(item["result"] == "pending" for item in result["reader_review"].values()))
        self.save_review("chapter", result)
        self.assert_quality_block("coverage_pending")

    def test_manuscript_change_invalidates_review(self):
        with (self.root / "manuscript/chapter.md").open("a", encoding="utf-8") as stream:
            stream.write("新增一句行为说明。\n")
        self.assert_quality_block("stale_input")

    def test_every_rule_is_fingerprinted(self):
        for path in gate.RULE_PATHS:
            with self.subTest(path=path):
                original = (self.root / path).read_text(encoding="utf-8")
                (self.root / path).write_text(original + "新约定。", encoding="utf-8")
                self.assert_quality_block("stale_input")
                (self.root / path).write_text(original, encoding="utf-8")

    def test_canonical_fact_change_invalidates_review(self):
        self.facts[0]["claim"] = "修订后的范围"
        self.save("facts.yaml", self.facts)
        self.assert_quality_block("stale_input")

    def test_recursive_prerequisite_change_invalidates_review(self):
        self.book["units"].append({"id": "grand", "path": "manuscript/grand.md", "facts": [], "prerequisites": []})
        self.book["units"][0]["prerequisites"] = ["grand"]
        (self.root / "manuscript/grand.md").write_text("# 更早前置\n", encoding="utf-8")
        self.save("book.yaml", self.book)
        self.save_review("chapter", self.receipt())
        (self.root / "manuscript/grand.md").write_text("# 更早前置\n解释发生变化。", encoding="utf-8")
        self.assert_quality_block("stale_input")

    def test_tool_itself_is_fingerprinted(self):
        with (self.root / "tools/writing_gate.py").open("a", encoding="utf-8") as stream:
            stream.write("\n# altered policy\n")
        self.assert_quality_block("stale_input")

    def test_evidence_material_change_invalidates_review(self):
        (self.root / "example.txt").write_text("原始材料", encoding="utf-8")
        record = self.receipt()
        record["evidence_paths"] = ["example.txt"]
        self.save_review("chapter", self.refresh(record))
        self.assertEqual([], self.check(True, ["chapter"])["errors"])
        (self.root / "example.txt").write_text("改变后的材料", encoding="utf-8")
        self.assert_quality_block("stale_input")

    def test_unsafe_evidence_paths_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "secret.txt"
            external.write_text("outside", encoding="utf-8")
            (self.root / "linked.txt").symlink_to(external)
            for path in ("../secret.txt", str(external), "linked.txt", "checks/writing/chapter.yaml", "C:\\secret.txt"):
                with self.subTest(path=path):
                    record = self.receipt()
                    record["evidence_paths"] = [path]
                    self.save_review("chapter", record)
                    self.assertIn("invalid_input", self.codes(self.check(units=["chapter"])))

    def test_fake_official_domain_is_rejected_even_when_registered(self):
        for url in ("https://learn.chatgpt.com.evil.test/docs/projects", "https://learn.chatgpt.com@evil.test/", "http://learn.chatgpt.com/docs/projects", "https://github.com/openai/codex-evil", "https://github.com/openai/codex/%2e%2e/other"):
            with self.subTest(url=url):
                self.facts[0]["sources"][0]["url"] = url
                self.save("facts.yaml", self.facts)
                self.save_review("chapter", self.receipt())
                self.assertIn("unofficial_claim_url", self.codes(self.check(units=["chapter"])))

    def test_supported_domain_does_not_make_unverified_claim_pass(self):
        for state in ("partial", "conflict", "unverified"):
            with self.subTest(state=state):
                record = self.receipt()
                record["fact_review"]["claims"][0]["support"] = state
                self.save_review("chapter", record)
                self.assert_quality_block("unsupported_claim")

    def test_claim_url_must_match_canonical_fact_source(self):
        record = self.receipt()
        record["fact_review"]["claims"][0]["url"] = "https://learn.chatgpt.com/docs/other"
        self.save_review("chapter", record)
        self.assertIn("unregistered_source", self.codes(self.check(units=["chapter"])))

    def test_missing_fact_coverage_blocks_reviewed_claims(self):
        extra = dict(self.facts[0], id="second-fact")
        self.facts.append(extra)
        self.book["units"][1]["facts"].append("second-fact")
        self.save("facts.yaml", self.facts)
        self.save("book.yaml", self.book)
        self.save_review("chapter", self.receipt())
        self.assert_quality_block("missing_fact_coverage")

    def test_unused_fact_needs_explicit_reason_and_cannot_also_be_used(self):
        record = self.receipt()
        record["fact_review"]["unused_facts"] = {"local-project": "当前稿并未涉及此事实"}
        self.save_review("chapter", record)
        self.assertIn("contradictory_coverage", self.codes(self.check(units=["chapter"])))
        record["fact_review"]["claims"] = []
        self.save_review("chapter", record)
        self.assertEqual([], self.check(True, ["chapter"])["errors"])
        record["fact_review"]["unused_facts"]["local-project"] = ""
        self.save_review("chapter", record)
        self.assertIn("invalid_unused_facts", self.codes(self.check(units=["chapter"])))

    def test_docs_must_not_predate_this_writing_batch(self):
        record = self.receipt()
        record["fact_review"]["claims"][0]["checked_on"] = (self.day - timedelta(days=1)).isoformat()
        self.save_review("chapter", record)
        self.assert_quality_block("documents_before_batch")

    def test_normal_cross_day_writing_and_review_reuses_batch_reading(self):
        record = self.receipt()
        start = (self.day - timedelta(days=1)).isoformat()
        record["batch_started_on"] = start
        record["fact_review"]["docs_checked_on"] = start
        record["fact_review"]["change_review"]["checked_on"] = start
        record["fact_review"]["claims"][0]["checked_on"] = start
        self.save_review("chapter", record)
        self.assertEqual([], self.check(True, ["chapter"])["errors"])

    def test_reading_after_completed_review_is_a_structural_error(self):
        record = self.receipt()
        record["batch_started_on"] = (self.day - timedelta(days=2)).isoformat()
        record["reviewed_on"] = (self.day - timedelta(days=1)).isoformat()
        self.save_review("chapter", record)
        self.assertIn("documents_after_review", self.codes(self.check(units=["chapter"])))

    def test_batch_cannot_start_after_completed_review(self):
        record = self.receipt()
        record["reviewed_on"] = (self.day - timedelta(days=1)).isoformat()
        self.save_review("chapter", record)
        self.assertIn("invalid_review_period", self.codes(self.check(units=["chapter"])))

    def test_batch_started_on_is_required(self):
        record = self.receipt()
        del record["batch_started_on"]
        self.save_review("chapter", record)
        self.assertIn("invalid_schema", self.codes(self.check(units=["chapter"])))

    def test_publication_rechecks_docs_and_update_within_seven_days(self):
        record = self.receipt()
        old = (self.day - timedelta(days=8)).isoformat()
        record["batch_started_on"] = old
        record["reviewed_on"] = old
        record["study_brief"]["prepared_on"] = old
        record["study_brief"]["official_readings"][0]["checked_on"] = old
        record["fact_review"]["docs_checked_on"] = old
        record["fact_review"]["change_review"]["checked_on"] = old
        record["fact_review"]["claims"][0]["checked_on"] = old
        self.save_review("chapter", record)
        self.assertEqual([], self.check(units=["chapter"])["errors"])
        self.assertIn("documents_expired", self.codes(self.check(True, ["chapter"])))

    def test_seven_day_boundary_is_inclusive(self):
        record = self.receipt()
        old = (self.day - timedelta(days=7)).isoformat()
        record["batch_started_on"] = old
        record["reviewed_on"] = old
        record["study_brief"]["prepared_on"] = old
        record["study_brief"]["official_readings"][0]["checked_on"] = old
        record["fact_review"]["docs_checked_on"] = old
        record["fact_review"]["change_review"]["checked_on"] = old
        record["fact_review"]["claims"][0]["checked_on"] = old
        self.save_review("chapter", record)
        self.assertEqual([], self.check(True, ["chapter"])["errors"])

    def test_future_dates_are_structural_errors(self):
        record = self.receipt()
        record["fact_review"]["change_review"]["checked_on"] = (self.day + timedelta(days=1)).isoformat()
        self.save_review("chapter", record)
        self.assertIn("future_date", self.codes(self.check(units=["chapter"])))

    def test_beta_requires_notice_in_the_actual_manuscript(self):
        record = self.receipt()
        record["fact_review"]["claims"][0]["availability"] = "beta"
        self.save_review("chapter", record)
        self.assert_quality_block("missing_availability_notice")
        self.commit_plan(record, mode="revision")
        notice = "此功能仍处于测试阶段。"
        with (self.root / "manuscript/chapter.md").open("a", encoding="utf-8") as stream:
            stream.write(notice)
        record["fact_review"]["claims"][0]["notice"] = notice
        self.save_review("chapter", self.refresh(record))
        self.assertEqual([], self.check(True, ["chapter"])["errors"])

    def test_selection_does_not_require_unselected_review_records(self):
        (self.root / "checks/writing/other.yaml").unlink()
        self.assertEqual([], self.check(True, ["chapter"])["errors"])
        self.assertIn("missing_writing_review", self.codes(self.check(True)))
        self.assertIn("invalid_writing_gate_input", self.codes(self.check(units=["missing"])))

    def test_zero_facts_still_requires_full_text_coverage_review(self):
        record = self.receipt("base")
        record["fact_review"]["coverage"] = "pending"
        self.save_review("base", record)
        self.assertIn("coverage_pending", self.codes(self.check(True, ["base"])))

    def test_wrong_product_surface_and_explicit_fact_scope_are_rejected(self):
        record = self.receipt()
        record["fact_review"]["claims"][0]["surface"] = "os"
        self.save_review("chapter", record)
        self.assertIn("scope_mismatch", self.codes(self.check(units=["chapter"])))
        self.facts[0]["scope"]["surface"] = "codex-cli"
        self.save("facts.yaml", self.facts)
        self.save_review("chapter", self.receipt())
        self.assertIn("scope_mismatch", self.codes(self.check(units=["chapter"])))

    def test_os_facts_accept_only_declared_vendor_and_platform(self):
        self.facts[0].update(id="windows-unzip", scope={"product": "windows", "surface": "os", "platforms": ["windows"]})
        self.facts[0]["sources"][0]["url"] = "https://support.microsoft.com/windows/zip"
        self.book["units"][1]["facts"] = ["windows-unzip"]
        self.save("facts.yaml", self.facts)
        self.save("book.yaml", self.book)
        record = self.receipt()
        claim = record["fact_review"]["claims"][0]
        claim.update(fact_id="windows-unzip", surface="os", platforms=["windows"])
        self.save_review("chapter", record)
        self.assertEqual([], self.check(True, ["chapter"])["errors"])
        claim["platforms"] = ["mac"]
        self.save_review("chapter", record)
        self.assertIn("scope_mismatch", self.codes(self.check(units=["chapter"])))

    def test_duplicate_fact_id_and_yaml_key_are_not_silently_overwritten(self):
        self.facts.append(dict(self.facts[0]))
        self.save("facts.yaml", self.facts)
        self.assertIn("invalid_writing_gate_input", self.codes(self.check()))
        self.save("facts.yaml", self.facts[:1])
        with (self.root / "checks/writing/chapter.yaml").open("a", encoding="utf-8") as stream:
            stream.write("unit: chapter\n")
        self.assertIn("invalid_writing_gate_input", self.codes(self.check(units=["chapter"])))

    def test_invalid_schema_or_unknown_field_is_structural(self):
        record = self.receipt()
        record["schema"] = True
        record["automatic_semantic_pass"] = True
        self.save_review("chapter", record)
        self.assertIn("invalid_schema", self.codes(self.check(units=["chapter"])))

    def test_anchor_must_exist_once_and_duplicate_claims_are_rejected(self):
        record = self.receipt()
        record["fact_review"]["claims"].append(dict(record["fact_review"]["claims"][0]))
        self.save_review("chapter", record)
        self.assertIn("duplicate_claim", self.codes(self.check(units=["chapter"])))
        record["fact_review"]["claims"] = record["fact_review"]["claims"][:1]
        record["fact_review"]["claims"][0]["anchor"] = "正文中不存在"
        self.save_review("chapter", record)
        self.assertIn("invalid_anchor", self.codes(self.check(units=["chapter"])))

    def test_missing_required_rule_blocks_even_drafting(self):
        (self.root / "写作约定.md").unlink()
        self.assertIn("invalid_writing_gate_input", self.codes(self.check()))

    def test_cli_prepare_outputs_pending_yaml_and_never_mutates_records(self):
        before = (self.root / "checks/writing/chapter.yaml").read_bytes()
        result = subprocess.run([sys.executable, str(SOURCE), "--root", str(self.root), "prepare", "--unit", "chapter"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("pending", yaml.safe_load(result.stdout)["fact_review"]["coverage"])
        for expected in ("有电脑经验，无编程经验", "manuscript/base.md", "local-project", "可连接本地目录", "https://learn.chatgpt.com/docs/projects", "unspecified", "baseline:", "platforms:"):
            self.assertIn(expected, result.stdout)
        self.assertEqual(before, (self.root / "checks/writing/chapter.yaml").read_bytes())

    def test_unspecified_maturity_does_not_add_repetitive_reader_disclaimers(self):
        record = self.receipt()
        claim = record["fact_review"]["claims"][0]
        claim["availability"] = "unspecified"
        claim["conditions"] = "官方原页未标明成熟度，不能据当前页面推断稳定状态。"
        self.save_review("chapter", record)
        self.assertEqual([], self.check(True, ["chapter"])["errors"])

    def test_malformed_claim_fields_return_structured_errors(self):
        for field in ("surface", "availability", "support", "url", "conditions", "rationale"):
            for value in (["invalid"], {"invalid": "value"}):
                with self.subTest(field=field, value=value):
                    record = self.receipt()
                    record["fact_review"]["claims"][0][field] = value
                    self.save_review("chapter", record)
                    self.assertIn("invalid_claim_schema", self.codes(self.check(units=["chapter"])))

    def test_malformed_canonical_source_or_scope_returns_structured_error(self):
        for change in ("source", "product"):
            with self.subTest(change=change):
                original = yaml.safe_load(yaml.safe_dump(self.facts))
                if change == "source":
                    self.facts[0]["sources"][0]["url"] = {"url": "wrong type"}
                else:
                    self.facts[0]["scope"]["product"] = ["codex"]
                self.save("facts.yaml", self.facts)
                self.assertIn("invalid_writing_gate_input", self.codes(self.check(units=["chapter"])))
                self.facts = original
                self.save("facts.yaml", self.facts)

    def test_malformed_dates_return_structured_error(self):
        for value in ([], {}, 1234, "20260926", "2026-W39-6"):
            with self.subTest(value=value):
                record = self.receipt()
                record["reviewed_on"] = value
                self.save_review("chapter", record)
                self.assertIn("invalid_date", self.codes(self.check(units=["chapter"])))

    def test_cli_check_returns_nonzero_for_publication_gap(self):
        (self.root / "checks/writing/chapter.yaml").unlink()
        result = subprocess.run([sys.executable, str(SOURCE), "--root", str(self.root), "check", "--units", "chapter", "--publication"], capture_output=True, text=True)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("missing_writing_review", self.codes(json.loads(result.stdout)))


    def commit_plan(self, record, mode="new_draft"):
        def git(*args):
            result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(self.root), *args], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            return result.stdout.strip()
        git("init", "--quiet")
        git("config", "user.name", "Gate Test")
        git("config", "user.email", "gate@example.invalid")
        record["study_brief"]["mode"] = mode
        record["study_commit"] = None
        self.save_review(record["unit"], record)
        git("add", ".")
        git("commit", "--quiet", "-m", "Save actual before-writing plan and starting manuscript")
        record["study_commit"] = git("rev-parse", "HEAD")
        return git

    def test_schema_one_is_historical_not_a_current_pass(self):
        record = self.receipt()
        record["schema"] = 1
        for key in ("study_brief", "study_commit", "study_review"):
            record.pop(key)
        self.save_review("chapter", record)
        self.assert_quality_block("writing_schema_migration")
        self.assertEqual({"editorial": "unknown", "facts": "unknown"}, self.check(units=["chapter"])["quality"]["chapter"])

    def test_missing_study_skeleton_is_pending_not_an_invented_pass(self):
        record = self.receipt()
        del record["study_brief"]
        self.save_review("chapter", record)
        self.assert_quality_block("study_brief_pending")
        result = self.check(True, ["chapter"])
        self.assertEqual("unknown", result["quality"]["chapter"]["editorial"])
        self.assertEqual("pass", result["quality"]["chapter"]["facts"])

    def test_study_needs_understanding_and_teaching_use(self):
        record = self.receipt()
        record["study_brief"]["official_readings"][0]["understanding"] = ""
        self.save_review("chapter", record)
        self.assert_quality_block("study_readings_pending")

    def test_study_read_after_declared_plan_is_rejected(self):
        record = self.receipt()
        record["study_brief"]["prepared_on"] = (self.day - timedelta(days=1)).isoformat()
        self.save_review("chapter", record)
        self.assertIn("study_reading_after_plan", self.codes(self.check(units=["chapter"])))

    def test_study_uses_registered_official_sources(self):
        record = self.receipt()
        record["study_brief"]["official_readings"][0]["url"] = "https://example.invalid/learn"
        self.save_review("chapter", record)
        self.assertIn("unofficial_study_url", self.codes(self.check(units=["chapter"])))
        self.assertIn("unregistered_study_source", self.codes(self.check(units=["chapter"])))

    def test_new_draft_and_revision_require_a_real_saved_plan(self):
        for mode in ("new_draft", "revision"):
            with self.subTest(mode=mode):
                record = self.receipt()
                record["study_brief"]["mode"] = mode
                self.save_review("chapter", record)
                self.assert_quality_block("study_commit_pending")

    def test_saved_plan_committed_before_actual_prose_change_is_accepted(self):
        record = self.receipt()
        self.commit_plan(record)
        with (self.root / "manuscript/chapter.md").open("a", encoding="utf-8") as stream:
            stream.write("先从读者已有的材料出发。\n")
        self.save_review("chapter", self.refresh(record))
        result = self.check(True, ["chapter"])
        self.assertEqual([], result["errors"], result)
        self.assertEqual({"editorial": "pass", "facts": "pass"}, result["quality"]["chapter"])

    def test_editing_plan_after_saved_commit_does_not_prove_before_writing(self):
        record = self.receipt()
        self.commit_plan(record)
        record["study_brief"]["case_plan"] = "稿后替换原有案例方案。"
        self.save_review("chapter", self.refresh(record))
        self.assert_quality_block("study_plan_changed")

    def test_invented_base_hash_in_saved_plan_is_rejected(self):
        record = self.receipt()
        record["study_brief"]["base_manuscript_sha256"] = "0" * 64
        self.commit_plan(record)
        self.save_review("chapter", self.refresh(record))
        self.assert_quality_block("study_base_changed")

    def test_plan_commit_must_be_an_actual_ancestor(self):
        record = self.receipt()
        git = self.commit_plan(record)
        git("checkout", "--quiet", "--orphan", "unrelated")
        git("commit", "--quiet", "-m", "Unrelated history")
        self.save_review("chapter", self.refresh(record))
        self.assert_quality_block("study_commit_unavailable")

    def test_retrospective_study_is_explicit_and_cannot_claim_original_order(self):
        result = self.check(True, ["chapter"])
        self.assertEqual([], result["errors"])
        self.assertIn("retrospective_study", self.codes(result, "warnings"))
        record = self.receipt()
        self.commit_plan(record, mode="retrospective")
        self.save_review("chapter", self.refresh(record))
        self.assertEqual([], self.check(True, ["chapter"])["errors"])

    def test_study_and_reader_failures_only_fail_editorial_quality(self):
        record = self.receipt()
        record["study_review"]["result"] = "fail"
        self.save_review("chapter", record)
        self.assertEqual({"editorial": "fail", "facts": "pass"}, self.check(True, ["chapter"])["quality"]["chapter"])
        record["study_review"]["result"] = "pass"
        record["reader_review"]["concept_timing"]["result"] = "fail"
        self.save_review("chapter", record)
        self.assertEqual("fail", self.check(True, ["chapter"])["quality"]["chapter"]["editorial"])

    def test_fact_failure_only_fails_facts_quality_and_staleness_affects_both(self):
        record = self.receipt()
        record["fact_review"]["claims"][0]["support"] = "conflict"
        self.save_review("chapter", record)
        self.assertEqual({"editorial": "pass", "facts": "fail"}, self.check(True, ["chapter"])["quality"]["chapter"])
        (self.root / "manuscript/chapter.md").write_text("正文已变化。\n", encoding="utf-8")
        self.assertEqual({"editorial": "stale", "facts": "stale"}, self.check(True, ["chapter"])["quality"]["chapter"])

    def test_irrelevant_rules_and_baseline_date_do_not_invalidate_review(self):
        before = gate.fingerprint(self.root, "chapter")
        for name in ("AGENTS.md", "需求文档.md", "内容框架.md"):
            (self.root / name).write_text("新增品牌内容或目录空格。\n", encoding="utf-8")
        self.book["baseline"]["checked_on"] = "2026-09-27"
        self.book["branding"] = {"name": "author"}
        self.save("book.yaml", self.book)
        self.assertEqual(before, gate.fingerprint(self.root, "chapter"))
        self.assertEqual([], self.check(True, ["chapter"])["errors"])

    def test_ignorable_markdown_tail_whitespace_does_not_invalidate_review(self):
        before = gate.fingerprint(self.root, "chapter")
        path = self.root / "写作约定.md"
        path.write_text(path.read_text().rstrip() + " \n\n  ", encoding="utf-8")
        self.assertEqual(before, gate.fingerprint(self.root, "chapter"))
        path.write_text(path.read_text() + "规则含义改变。", encoding="utf-8")
        self.assertNotEqual(before, gate.fingerprint(self.root, "chapter"))

    def test_markdown_hard_break_inside_text_is_not_erased(self):
        before = gate.fingerprint(self.root, "chapter")
        path = self.root / "manuscript/chapter.md"
        path.write_text(path.read_text().replace("Codex。\n", "Codex。  \n"), encoding="utf-8")
        self.assertNotEqual(before, gate.fingerprint(self.root, "chapter"))

    def test_only_relevant_source_authorities_are_fingerprinted(self):
        before = gate.fingerprint(self.root, "chapter")
        data = yaml.safe_load((self.root / gate.AUTHORITY_PATH).read_text())
        data["products"]["git"]["hosts"].append("www.git-scm.com")
        self.save(gate.AUTHORITY_PATH, data)
        self.assertEqual(before, gate.fingerprint(self.root, "chapter"))
        data["products"]["codex"]["hosts"].append("openai.com")
        self.save(gate.AUTHORITY_PATH, data)
        self.assertNotEqual(before, gate.fingerprint(self.root, "chapter"))

    def test_git_source_authority_can_be_declared_without_code_changes(self):
        self.facts[0]["scope"] = {"product": "git", "surfaces": ["os", "other"]}
        self.facts[0]["sources"][0]["url"] = "https://git-scm.com/downloads/win"
        self.save("facts.yaml", self.facts)
        record = self.receipt()
        record["fact_review"]["claims"][0]["surface"] = "os"
        self.save_review("chapter", record)
        self.assertEqual([], self.check(True, ["chapter"])["errors"])

    def test_fact_surfaces_list_is_enforced(self):
        self.facts[0]["scope"]["surfaces"] = ["codex-cli"]
        self.save("facts.yaml", self.facts)
        self.save_review("chapter", self.receipt())
        self.assertIn("scope_mismatch", self.codes(self.check(units=["chapter"])))

    def test_github_repository_boundaries_remain_strict(self):
        good = "https://github.com/openai/codex/releases/tag/example"
        self.facts[0]["sources"][0]["url"] = good
        self.save("facts.yaml", self.facts)
        self.save_review("chapter", self.receipt())
        self.assertEqual([], self.check(True, ["chapter"])["errors"])
        for bad in ("https://github.com/another/codex", "https://github.com/openai/codex-evil", "https://github.com/openai/codex/%252e%252e/other", "https://github.com/openai/codex/%2e%2e/other"):
            self.facts[0]["sources"][0]["url"] = bad
            self.save("facts.yaml", self.facts)
            self.save_review("chapter", self.receipt())
            self.assertIn("unofficial_claim_url", self.codes(self.check(units=["chapter"])))

    def test_authority_table_cannot_allow_all_github_repositories(self):
        data = yaml.safe_load((self.root / gate.AUTHORITY_PATH).read_text())
        data["products"]["codex"]["hosts"].append("github.com")
        self.save(gate.AUTHORITY_PATH, data)
        self.assertIn("invalid_writing_gate_input", self.codes(self.check()))

    def test_headings_are_not_claim_anchors_with_or_without_hash(self):
        for anchor in ("# 本章", "本章"):
            record = self.receipt()
            record["fact_review"]["claims"][0]["anchor"] = anchor
            self.save_review("chapter", record)
            self.assertIn("invalid_anchor", self.codes(self.check(units=["chapter"])))

    def reused_reading(self, days=1):
        record = self.receipt()
        old = (self.day - timedelta(days=days)).isoformat()
        record["fact_review"]["docs_checked_on"] = old
        record["fact_review"]["claims"][0]["checked_on"] = old
        record["fact_review"]["reading_reuse"] = [{
            "url": self.facts[0]["sources"][0]["url"], "checked_on": old,
            "from_batch": "previous-writing-batch", "reason": "连续工作中的同一官方小节，原日期保留；本批另读更新日志。",
        }]
        return record

    def test_exact_recent_reading_can_be_reused_without_changing_its_date(self):
        record = self.reused_reading()
        self.save_review("chapter", record)
        result = self.check(True, ["chapter"])
        self.assertEqual([], result["errors"], result)
        self.assertEqual("2026-09-25", record["fact_review"]["claims"][0]["checked_on"])

    def test_reuse_does_not_cover_a_different_date(self):
        record = self.reused_reading()
        record["fact_review"]["reading_reuse"][0]["checked_on"] = "2026-09-24"
        self.save_review("chapter", record)
        self.assert_quality_block("documents_before_batch")

    def test_reusing_eight_day_old_reading_still_expires(self):
        self.save_review("chapter", self.reused_reading(days=8))
        self.assert_quality_block("documents_expired")

    def test_update_review_cannot_be_reused_from_a_previous_batch(self):
        record = self.reused_reading()
        url = record["fact_review"]["change_review"]["url"]
        record["fact_review"]["change_review"]["checked_on"] = "2026-09-25"
        self.facts[0]["sources"].append({"url": url, "checked_on": "2026-09-25"})
        self.save("facts.yaml", self.facts)
        record["fact_review"]["reading_reuse"].append({"url": url, "checked_on": "2026-09-25", "from_batch": "yesterday", "reason": "不能用昨天更新页冒充今天复查"})
        self.save_review("chapter", self.refresh(record))
        self.assert_quality_block("documents_before_batch")

    def test_live_publication_uses_today_instead_of_freezing_a_release_date(self):
        record = self.receipt()
        self.save_review("chapter", record)
        self.book["published"] = {"date": self.day.isoformat()}
        self.save("book.yaml", self.book)
        result = gate.validate(self.root, ["chapter"], publication=True, today=self.day + timedelta(days=8))
        self.assertIn("documents_expired", self.codes(result))

    def test_all_unused_with_product_risk_words_gets_a_manual_review_notice(self):
        record = self.receipt()
        with (self.root / "manuscript/chapter.md").open("a", encoding="utf-8") as stream:
            stream.write("Codex 绝不会越过权限边界。\n")
        record["fact_review"]["claims"] = []
        record["fact_review"]["unused_facts"] = {"local-project": "人工声明本章未用；工具不能代替核对这个判断。"}
        self.save_review("chapter", self.refresh(record))
        self.assertIn("unused_product_claims_review", self.codes(self.check(True, ["chapter"]), "warnings"))

    def test_review_packet_shows_entire_text_and_does_not_certify_coverage(self):
        path = self.root / "manuscript/chapter.md"
        path.write_text(path.read_text() + "Codex 默认自动执行。\n普通正文也必须读。\n", encoding="utf-8")
        before = (self.root / "checks/writing/chapter.yaml").read_bytes()
        result = subprocess.run([sys.executable, str(SOURCE), "--root", str(self.root), "review-packet", "--unit", "chapter"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        for text in ("普通正文也必须读。", "非穷尽", "默认", "https://learn.chatgpt.com/docs/projects", "1 | # 本章"):
            self.assertIn(text, result.stdout)
        self.assertEqual(before, (self.root / "checks/writing/chapter.yaml").read_bytes())


    def test_new_chapter_cannot_use_retrospective_migration(self):
        gate.MIGRATION_SNAPSHOTS.pop("chapter")
        self.assert_quality_block("retrospective_not_eligible")

    def test_legacy_sample_substantive_revision_requires_a_new_study_plan(self):
        record = self.receipt()
        with (self.root / "manuscript/chapter.md").open("a", encoding="utf-8") as stream:
            stream.write("改变任务目标和本章解释。\n")
        record["study_brief"]["base_manuscript_sha256"] = gate._digest(self.root / "manuscript/chapter.md")
        self.save_review("chapter", self.refresh(record))
        self.assert_quality_block("retrospective_not_eligible")

    def test_stale_reading_cannot_be_presented_as_new_study(self):
        record = self.receipt()
        record["study_brief"]["official_readings"][0]["checked_on"] = (self.day - timedelta(days=8)).isoformat()
        self.save_review("chapter", record)
        self.assert_quality_block("study_reading_expired")

    def test_current_worktree_warns_but_fixed_committed_inputs_do_not(self):
        record = self.receipt()
        git = self.commit_plan(record)
        self.save_review("chapter", record)
        path = self.root / "manuscript/chapter.md"
        path.write_text(path.read_text() + "本批实际修订。\n", encoding="utf-8")
        self.save_review("chapter", self.refresh(record))
        self.assertIn("uncommitted_review_inputs", self.codes(self.check(True, ["chapter"]), "warnings"))
        git("add", ".")
        git("commit", "--quiet", "-m", "Save reviewed source and current record")
        result = self.check(True, ["chapter"])
        self.assertEqual([], result["errors"], result)
        self.assertNotIn("uncommitted_review_inputs", self.codes(result, "warnings"))


if __name__ == "__main__":
    unittest.main()
