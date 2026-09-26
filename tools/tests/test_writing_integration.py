"""Codex writing integration boundaries; fixtures are not factual or GUI evidence."""
from contextlib import redirect_stderr
from datetime import date
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from studio_lib.checker import check_book
from studio_lib.common import StudioError


def load_tool(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


studio = load_tool("writing_integration_studio", TOOLS / "studio.py")
DELIVERY_PATH = TOOLS / "check_delivery.py"
if not DELIVERY_PATH.is_file():
    DELIVERY_PATH = TOOLS.parent / "books/codex/tools/check_delivery.py"
delivery = load_tool("writing_integration_delivery", DELIVERY_PATH)

# Deliberately controlled return values isolate the Studio/gate contract.
# Real study/claim validation has its own tests in test_writing_gate.py.
GATE_FIXTURE = """import json
from pathlib import Path

def validate(root, units=None, publication=False, today=None):
    states = json.loads((Path(root) / 'fixture-writing.json').read_text())
    result = {'errors': [], 'warnings': [], 'quality': {}}
    for uid in units:
        quality = states.get(uid, {'editorial': 'unknown', 'facts': 'unknown'})
        result['quality'][uid] = quality
        for kind, state in quality.items():
            if state != 'pass':
                result['errors' if publication else 'warnings'].append({
                    'code': 'fixture_' + kind, 'path': 'checks/writing/' + uid + '.yaml',
                    'message': uid + ' ' + kind + ' is ' + state})
    return result
"""


class RepoFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="writing integration 中文 ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "book"
        self.root.mkdir()
        self.today = date(2026, 9, 26)
        self.book = {
            "id": "codex", "title": "Fictional integration book", "type": "tutorial",
            "language": "zh-CN", "product": "example", "is_test": True,
            "baseline": {"product_version": "fixture-1", "checked_on": self.today.isoformat()},
            "units": [self.unit("intro")], "outputs": {}, "toolkit": "0.1.0",
            "platforms": ["windows", "mac"],
            "required_checks": ["editorial", "facts", "operations"],
        }
        self.put("manuscript/intro.md", "# Fictional chapter\n\nAn independently authored fixture.\n")
        self.save("book.yaml", self.book)
        self.save("facts.yaml", [])
        self.put("tools/writing_gate.py", GATE_FIXTURE)
        self.gate_states({"intro": {"editorial": "pass", "facts": "pass"}})
        self.git("init", "-q")
        self.git("config", "user.name", "Fictional Tester")
        self.git("config", "user.email", "fixture@example.invalid")
        self.source = self.commit()

    @staticmethod
    def unit(uid):
        return {"id": uid, "title": uid, "path": "manuscript/" + uid + ".md",
                "depth": 0, "section": "popular", "prerequisites": [], "facts": [], "features": []}

    def put(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def save(self, relative, value):
        self.put(relative, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "Fictional integration fixture")
        return self.git("rev-parse", "HEAD")

    def gate_states(self, value):
        self.put("fixture-writing.json", json.dumps(value))

    def review(self, kind, result="pass", platforms=None, unit="intro"):
        return {"unit": unit, "source_commit": self.source,
                "paths": ["manuscript/" + unit + ".md"], "kind": kind,
                "result": result, "checked_on": self.today.isoformat(),
                "engine": "codex", "author_engine": "codex",
                "platforms": platforms if platforms is not None else ["windows", "mac"]}

    def run_check(self, publication=True, scope=None):
        return check_book(self.root, publication=publication, scope=scope,
                          today=self.today, freshness=False)


class WritingQualityIntegrationTests(RepoFixture):
    def test_legacy_pass_cannot_replace_missing_new_editorial_or_facts(self):
        self.save("checks/legacy.yaml", [self.review(kind) for kind in ("editorial", "facts", "operations")])
        self.gate_states({})
        result = self.run_check()
        self.assertFalse(result["ok"], result)
        quality = result["summary"]["quality"]["intro"]
        self.assertEqual(quality["editorial"], "unknown")
        self.assertEqual(quality["facts"], "unknown")
        self.assertEqual(quality["operations"], "pass")
        codes = {issue["code"] for issue in result["issues"] if issue["level"] == "error"}
        self.assertTrue({"fixture_editorial", "fixture_facts"}.issubset(codes), result)

    def test_new_editorial_and_facts_replace_old_fail_without_filling_operations(self):
        self.save("checks/legacy.yaml", [self.review("editorial", "fail"), self.review("facts", "fail")])
        result = self.run_check()
        quality = result["summary"]["quality"]["intro"]
        self.assertEqual(quality["editorial"], "pass")
        self.assertEqual(quality["facts"], "pass")
        self.assertEqual(quality["operations"], "unknown")
        self.assertFalse(result["ok"], result)
        errors = [issue for issue in result["issues"] if issue["level"] == "error"]
        self.assertTrue(any(issue["code"] == "missing_review" and "operations" in issue["message"] for issue in errors), result)
        self.assertFalse(any(issue["code"] == "review_fail" for issue in errors), result)

    def test_operations_keep_actual_status_and_platform_coverage(self):
        for declared, platforms, expected in (
            ("unknown", ["windows", "mac"], "unknown"),
            ("fail", ["windows", "mac"], "fail"),
            ("pass", ["mac"], "unknown"),
            ("pass", ["windows", "mac"], "pass"),
        ):
            with self.subTest(declared=declared, platforms=platforms):
                self.save("checks/operations.yaml", [self.review("operations", declared, platforms)])
                result = self.run_check()
                quality = result["summary"]["quality"]["intro"]
                self.assertEqual(quality["operations"], expected, result)
                self.assertEqual(result["ok"], expected == "pass", result)
                self.assertEqual(quality["editorial"], "pass")

    def test_changed_source_still_invalidates_legacy_operations(self):
        self.save("checks/operations.yaml", [self.review("operations")])
        self.put("manuscript/intro.md", "# Changed operation\n\nThe procedure now differs.\n")
        result = self.run_check()
        self.assertEqual(result["summary"]["quality"]["intro"]["operations"], "stale")
        self.assertEqual(result["summary"]["quality"]["intro"]["editorial"], "pass")
        self.assertFalse(result["ok"], result)

    def test_partial_delivery_keeps_unselected_draft_gaps_as_warnings(self):
        self.book["units"].append(self.unit("unwritten"))
        self.put("manuscript/unwritten.md", "# Unwritten fixture\n\nDraft only.\n")
        self.save("book.yaml", self.book)
        self.source = self.commit()
        self.save("checks/operations.yaml", [self.review("operations")])
        scoped = self.run_check(scope=["intro"])
        self.assertTrue(scoped["ok"], scoped)
        self.assertFalse(any(issue["level"] == "error" for issue in scoped["issues"]), scoped)
        self.assertTrue(any(issue["code"] == "fixture_facts" and "unwritten" in issue["path"]
                            and issue["level"] == "warning" for issue in scoped["issues"]), scoped)
        self.assertEqual(scoped["summary"]["quality"]["unwritten"]["editorial"], "unknown")
        whole = self.run_check()
        self.assertFalse(whole["ok"], whole)
        self.assertTrue(any(issue["code"] == "fixture_facts" and "unwritten" in issue["path"]
                            and issue["level"] == "error" for issue in whole["issues"]), whole)

    def test_other_books_keep_old_review_mechanism_and_do_not_load_codex_gate(self):
        self.book["id"] = "another-book"
        self.save("book.yaml", self.book)
        self.put("tools/writing_gate.py", "raise AssertionError('must not load the Codex gate')\n")
        self.source = self.commit()
        self.save("checks/legacy.yaml", [self.review(kind) for kind in ("editorial", "facts", "operations")])
        result = self.run_check()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["quality"]["intro"]["editorial"], "pass")
        self.save("checks/legacy.yaml", [self.review("facts"), self.review("operations")])
        missing = self.run_check()
        self.assertFalse(missing["ok"], missing)
        self.assertEqual(missing["summary"]["quality"]["intro"]["editorial"], "unknown")


class FrozenSourceIntegrationTests(RepoFixture):
    def freeze_script(self, source):
        self.put("tools/studio.py", source)
        return self.commit()

    def frozen_reply(self, ok):
        return self.freeze_script(
            "import json, pathlib, sys\n"
            "report = {'ok': " + repr(ok) + ", 'issues': [], 'summary': {"
            "'book': 'codex', 'marker': 'fixed commit', 'argv': sys.argv[1:], 'cwd': str(pathlib.Path.cwd())}}\n"
            "print(json.dumps({'ok': report['ok'], 'results': [report]}))\n"
            "raise SystemExit(0 if report['ok'] else 1)\n")

    def test_fixed_pass_ignores_broken_worktree_and_uses_frozen_studio(self):
        source = self.frozen_reply(True)
        self.put("tools/studio.py", "raise AssertionError('dirty working tree executed')\n")
        self.book["id"] = "different-in-worktree"
        self.save("book.yaml", self.book)
        with patch.object(studio, "check", side_effect=AssertionError("current checker used")):
            report, checked = studio.source_gate(self.root, source, ["intro"], ["zh-CN"])
        self.assertTrue(report["ok"])
        self.assertEqual(checked, source)
        self.assertEqual(report["summary"]["marker"], "fixed commit")
        self.assertNotEqual(report["summary"]["cwd"], str(self.root))
        self.assertEqual(report["summary"]["argv"][-4:], ["--units", "intro", "--language", "zh-CN"])
        self.assertIn("--publication", report["summary"]["argv"])

    def test_fixed_failure_cannot_be_replaced_by_current_pass(self):
        source = self.frozen_reply(False)
        self.put("tools/studio.py", "print('working tree would pass')\n")
        with patch.object(studio, "check", return_value={"ok": True}) as current:
            report, checked = studio.source_gate(self.root, source, ["intro"], [])
        current.assert_not_called()
        self.assertFalse(report["ok"])
        self.assertEqual(checked, source)

    def test_missing_frozen_script_does_not_fall_back_to_worktree(self):
        source = self.source
        self.put("tools/studio.py", "print('not committed')\n")
        with patch.object(studio, "check", side_effect=AssertionError("fallback checker used")):
            with self.assertRaises(StudioError):
                studio.source_gate(self.root, source, ["intro"], [])

    def test_empty_scope_runs_whole_book_without_an_empty_units_option(self):
        source = self.frozen_reply(True)
        report, _ = studio.source_gate(self.root, source, [], [])
        self.assertTrue(report["ok"])
        self.assertNotIn("--units", report["summary"]["argv"])
        self.assertIn("--publication", report["summary"]["argv"])

    def test_invalid_json_from_frozen_script_is_rejected(self):
        source = self.freeze_script("print('this is not JSON')\n")
        with self.assertRaises(StudioError):
            studio.source_gate(self.root, source, ["intro"], [])

    def test_invalid_report_shapes_and_exit_status_are_rejected(self):
        good = {"ok": True, "issues": [], "summary": {"book": "codex"}}
        cases = [
            ([], 0),
            ({"ok": True, "results": []}, 0),
            ({"ok": True, "results": [good, good]}, 0),
            ({"ok": True, "results": [{"ok": "yes", "summary": {}}]}, 0),
            ({"ok": True, "results": [{"ok": True, "summary": None}]}, 0),
            ({"ok": 1, "results": [good]}, 0),
            ({"ok": True, "results": [{"ok": True, "issues": None, "summary": {"book": "codex"}}]}, 0),
            ({"ok": True, "results": [{"ok": True, "issues": [], "summary": {"book": "another-book"}}]}, 0),
            ({"ok": True, "results": [good]}, 1),
            ({"ok": False, "results": [good]}, 0),
        ]
        for index, (payload, code) in enumerate(cases):
            with self.subTest(payload=payload, code=code):
                text = json.dumps(payload)
                source = self.freeze_script("# case " + str(index) + "\nprint(" + repr(text) + ")\nraise SystemExit(" + str(code) + ")\n")
                with self.assertRaises(StudioError):
                    studio.source_gate(self.root, source, ["intro"], [])


class DeliveryScopeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.book = {"units": [{"id": "intro"}, {"id": "first-task"}]}

    def test_blank_unknown_duplicate_mixed_all_and_shell_syntax_fail(self):
        cases = ("", "   ", "unknown", "intro intro", "all intro", "intro all", "all all",
                 "intro; touch surprise", "$(touch surprise)", "intro && true", "--help")
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                delivery.delivery_units(raw, self.book)

    def test_valid_scope_preserves_requested_order_and_all_expands_registered_order(self):
        self.assertEqual(delivery.delivery_units(" first-task  intro ", self.book), ["first-task", "intro"])
        self.assertEqual(delivery.delivery_units("all", self.book), ["intro", "first-task"])
        self.assertEqual(delivery.delivery_units("intro", self.book), ["intro"])


    def test_delivery_main_rejects_input_before_execution_and_uses_argument_list(self):
        with tempfile.TemporaryDirectory(prefix="delivery scope ") as temp:
            root = Path(temp).resolve()
            (root / "book.yaml").write_text(yaml.safe_dump(self.book), encoding="utf-8")
            entry = root / "tools/check_delivery.py"
            with patch.object(delivery, "__file__", str(entry)), \
                 patch.object(delivery.subprocess, "call", return_value=1) as command:
                with patch.dict(delivery.os.environ, {"DELIVERY_UNITS": "intro; touch surprise"}), redirect_stderr(io.StringIO()):
                    self.assertEqual(delivery.main(), 2)
                command.assert_not_called()
                with patch.dict(delivery.os.environ, {"DELIVERY_UNITS": "first-task intro"}):
                    self.assertEqual(delivery.main(), 1)
                command.assert_called_once_with([
                    sys.executable, str(root / "tools/studio.py"), "--root", str(root),
                    "--json", "check", "--publication", "--units", "first-task", "intro"])


if __name__ == "__main__":
    unittest.main()
