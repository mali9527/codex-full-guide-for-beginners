"""Meaningful failure tests for the local writing review gate; no network calls."""
from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

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
        for uid in ("base", "chapter", "other"):
            self.save_review(uid, self.receipt(uid))

    def save(self, relative, value):
        (self.root / relative).write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def receipt(self, uid="chapter"):
        result = gate.prepare(self.root, uid, today=self.day)
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
        self.assertEqual({"errors": [], "warnings": []}, self.check(True))

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
        self.assertEqual({"errors": [], "warnings": []}, self.check(True, ["chapter"]))

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

    def test_legacy_os_facts_accept_only_corresponding_vendor_and_platform(self):
        self.facts[0].update(id="windows-unzip", scope={"product": "codex"})
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


if __name__ == "__main__":
    unittest.main()
