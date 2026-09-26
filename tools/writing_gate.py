#!/usr/bin/env python3
"""Local writing-evidence gate. It checks records, never verifies a source's meaning.

Product facts remain in facts.yaml. This module checks per-unit reviews against
those facts and a fingerprint of their actual inputs; it never fetches the web,
executes examples, writes pass records, or substitutes for platform tests.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

import yaml

RULE_PATHS = (
    "AGENTS.md", "写作约定.md", "需求文档.md", "内容框架.md",
    "术语翻译表.md", "案例设计.md", "写作核验机制.md",
)
READER_ITEMS = (
    "prior_knowledge", "concept_timing", "information_value", "natural_style",
    "localization", "independent_creation",
)
SURFACES = {"codex-desktop", "codex-cli", "os", "other"}
AVAILABILITIES = {"stable", "beta", "experimental", "deprecated", "unspecified"}
OPENAI_HOSTS = {"learn.chatgpt.com", "developers.openai.com", "platform.openai.com", "help.openai.com"}
MICROSOFT_HOSTS = {"support.microsoft.com", "learn.microsoft.com"}
APPLE_HOSTS = {"support.apple.com", "developer.apple.com"}
LEGACY_OS = {
    "windows-unzip": (MICROSOFT_HOSTS, {"windows"}),
    "mac-unzip": (APPLE_HOSTS, {"mac"}),
    "safari-download-location": (APPLE_HOSTS, {"mac"}),
}
MAX_PUBLICATION_AGE_DAYS = 7  # Book editorial policy, not an OpenAI guarantee.
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class GateError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise GateError("YAML key must be a scalar") from exc
        if duplicate:
            raise GateError("duplicate YAML key: " + str(key))
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _load(path):
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise GateError(str(path.name) + ": " + str(exc)) from exc


def _today(value=None):
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("not YYYY-MM-DD")
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise GateError("today must be a date or YYYY-MM-DD") from exc


def _safe(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise GateError("path must be a nonempty relative POSIX path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != relative or ":" in path.parts[0]:
        raise GateError("unsafe relative path: " + relative)
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise GateError("path escapes book root: " + relative) from exc
    if not resolved.is_file():
        raise GateError("missing input file: " + relative)
    return resolved


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value, field):
    if not isinstance(value, list) or not all(_text(x) for x in value) or len(value) != len(set(value)):
        raise GateError(field + " must be a list of unique nonempty strings")
    return value


def _official_url(value, hosts, allow_codex_github=False):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        decoded_path = unquote(parsed.path)
        if "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")):
            return False
        if parsed.hostname in hosts:
            return True
        return (allow_codex_github and parsed.hostname == "github.com"
                and (parsed.path == "/openai/codex" or parsed.path.startswith("/openai/codex/")))
    except ValueError:
        return False


def _authority(fact):
    scope = fact.get("scope")
    if not isinstance(scope, dict):
        raise GateError("fact scope must be a mapping: " + fact["id"])
    product = scope.get("product")
    if not _text(product):
        raise GateError("fact product must be nonempty text: " + fact["id"])
    if product in {"codex", "openai"}:
        # Only these three existing OS records predate explicit vendor products.
        if fact["id"] in LEGACY_OS:
            hosts, platforms = LEGACY_OS[fact["id"]]
            return hosts, False, {"os"}, platforms
        return OPENAI_HOSTS, True, ({"codex-desktop", "codex-cli"} if product == "codex" else {"codex-desktop", "codex-cli", "other"}), None
    if product in {"windows", "microsoft", "microsoft-windows"}:
        return MICROSOFT_HOSTS, False, {"os"}, {"windows"}
    if product in {"mac", "macos", "safari", "apple"}:
        return APPLE_HOSTS, False, {"os"}, {"mac"}
    raise GateError("unsupported official-source product: " + str(product))


class Context:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.book = _load(_safe(self.root, "book.yaml"))
        if not isinstance(self.book, dict):
            raise GateError("book.yaml must be a mapping")
        self.units = self._by_id(self.book.get("units"), "book units")
        self.facts = self._by_id(_load(_safe(self.root, "facts.yaml")), "facts")
        for fid, fact in self.facts.items():
            if not _text(fact.get("claim")) or not isinstance(fact.get("scope"), dict) or not _text(fact["scope"].get("product")):
                raise GateError(fid + " needs a claim and a product scope")
            sources = fact.get("sources")
            if not isinstance(sources, list) or any(not isinstance(source, dict) or not _text(source.get("url")) for source in sources):
                raise GateError(fid + " sources must be a list of mappings with text URLs")
        self.platforms = _strings(self.book.get("platforms"), "book platforms")
        for name in RULE_PATHS + ("tools/writing_gate.py",):
            _safe(self.root, name)
        for uid, unit in self.units.items():
            _safe(self.root, unit.get("path"))
            for field in ("facts", "prerequisites"):
                _strings(unit.get(field, []), uid + "." + field)
            missing = set(unit.get("facts", [])) - self.facts.keys()
            if missing:
                raise GateError(uid + " references unknown facts: " + ", ".join(sorted(missing)))
            missing = set(unit.get("prerequisites", [])) - self.units.keys()
            if missing:
                raise GateError(uid + " references unknown prerequisites: " + ", ".join(sorted(missing)))

    @staticmethod
    def _by_id(values, label):
        if not isinstance(values, list):
            raise GateError(label + " must be a list")
        result = {}
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not SLUG.fullmatch(value["id"]):
                raise GateError(label + " contains an invalid ID")
            if value["id"] in result:
                raise GateError(label + " contains duplicate ID: " + value["id"])
            result[value["id"]] = value
        return result

    def dependencies(self, uid):
        if uid not in self.units:
            raise GateError("unknown unit: " + str(uid))
        found, active = set(), set()

        def visit(current):
            if current in active:
                raise GateError("prerequisite cycle at " + current)
            if current in found:
                return
            active.add(current)
            for prerequisite in self.units[current].get("prerequisites", []):
                visit(prerequisite)
            active.remove(current)
            found.add(current)
        visit(uid)
        return sorted(found)

    def fingerprint(self, uid, evidence_paths=None):
        evidence_paths = _strings(evidence_paths if evidence_paths is not None else [], "evidence_paths")
        if any(p.startswith("checks/") or p == "checks" for p in evidence_paths):
            raise GateError("evidence_paths cannot include review records")
        deps = self.dependencies(uid)
        paths = set(RULE_PATHS + ("tools/writing_gate.py",))
        paths.update(self.units[d]["path"] for d in deps)
        paths.update(evidence_paths)
        fact_ids = sorted({fid for d in deps for fid in self.units[d].get("facts", [])})
        payload = {
            "schema": 1,
            "unit": uid,
            "book": {k: self.book.get(k) for k in ("id", "product", "language", "audience", "platforms", "baseline", "required_checks")},
            "units": {d: self.units[d] for d in deps},
            "facts": {fid: self.facts[fid] for fid in fact_ids},
            "files": {p: hashlib.sha256(_safe(self.root, p).read_bytes()).hexdigest() for p in sorted(paths)},
            "evidence_paths": sorted(evidence_paths),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def fingerprint(root, unit, evidence_paths=None):
    """Return the fingerprint; this does not certify that inputs were reviewed."""
    return Context(root).fingerprint(unit, evidence_paths)


def prepare(root, unit, today=None, evidence_paths=None):
    """Return an explicitly pending review template, never a pass receipt."""
    context = Context(root)
    day = _today(today).isoformat()
    record = {
        "schema": 1, "unit": unit, "input_sha256": context.fingerprint(unit, evidence_paths),
        "evidence_paths": list(evidence_paths or []), "batch_started_on": day,
        "reviewed_on": day, "reviewer": "codex",
        "reader_review": {name: {"result": "pending", "reason": "待按本章完整正文复核"} for name in READER_ITEMS},
        "fact_review": {
            "coverage": "pending", "reason": "待从完整正文反查产品断言；不能仅复用已有事实列表",
            "docs_checked_on": None,
            "change_review": {"url": "", "locator": "", "checked_on": None, "effect": ""},
            "claims": [], "unused_facts": {},
        },
    }
    return record


class Review:
    def __init__(self, context, uid, record, publication, today):
        self.context, self.uid, self.record = context, uid, record
        self.publication, self.today = publication, today
        self.path = "checks/writing/" + uid + ".yaml"
        self.errors, self.warnings = [], []
        self.reviewed = None
        self.batch_started = None

    def issue(self, code, message, structural=False):
        target = self.errors if structural or self.publication else self.warnings
        target.append({"code": code, "path": self.path, "message": message})

    def fields(self, obj, allowed, required, label):
        if not isinstance(obj, dict):
            self.issue("invalid_schema", label + " must be a mapping", True)
            return False
        unknown = set(obj) - set(allowed)
        missing = set(required) - set(obj)
        if unknown or missing:
            self.issue("invalid_schema", label + ": missing=" + str(sorted(missing)) + ", unknown=" + str(sorted(unknown, key=str)), True)
        return not missing

    def checked_date(self, value, label, required=True):
        if value is None and not required:
            self.issue("pending_date", label + " has not been checked")
            return None
        try:
            parsed = _today(value) if value is not None else None
        except GateError:
            parsed = None
        if parsed is None or isinstance(value, datetime):
            self.issue("invalid_date", label + " must be YYYY-MM-DD", True)
            return None
        if parsed > self.today:
            self.issue("future_date", label + " is in the future", True)
        return parsed

    def fresh(self, checked, label):
        if checked is None:
            return
        if self.batch_started and checked < self.batch_started:
            self.issue("documents_before_batch", label + " predates this writing batch")
        if self.reviewed and checked > self.reviewed:
            self.issue("documents_after_review", label + " postdates the completed review", True)
        if self.publication and (self.today - checked).days > MAX_PUBLICATION_AGE_DAYS:
            self.issue("documents_expired", label + " exceeds the book's 7-day publication window")

    def run(self):
        required = {"schema", "unit", "input_sha256", "batch_started_on", "reviewed_on", "reviewer", "reader_review", "fact_review"}
        if not self.fields(self.record, required | {"evidence_paths"}, required, "review"):
            return
        r = self.record
        if type(r["schema"]) is not int or r["schema"] != 1 or r["unit"] != self.uid:
            self.issue("invalid_schema", "schema must be 1 and unit must match its filename", True)
        if r["reviewer"] not in ("codex", "claude", "human"):
            self.issue("invalid_reviewer", "reviewer must truthfully identify codex, claude or human", True)
        self.batch_started = self.checked_date(r["batch_started_on"], "batch_started_on")
        self.reviewed = self.checked_date(r["reviewed_on"], "reviewed_on")
        if self.batch_started and self.reviewed and self.batch_started > self.reviewed:
            self.issue("invalid_review_period", "batch_started_on must not be later than reviewed_on", True)
        try:
            current = self.context.fingerprint(self.uid, r.get("evidence_paths", []))
            if not isinstance(r["input_sha256"], str) or not SHA256.fullmatch(r["input_sha256"]):
                self.issue("invalid_fingerprint", "input_sha256 must be a full SHA-256", True)
            elif r["input_sha256"] != current:
                self.issue("stale_input", "manuscript, prerequisites, facts, rules, materials or gate tool changed")
        except GateError as exc:
            self.issue("invalid_input", str(exc), True)
        reader = r["reader_review"]
        if self.fields(reader, READER_ITEMS, READER_ITEMS, "reader_review"):
            for name in READER_ITEMS:
                item = reader[name]
                if self.fields(item, {"result", "reason"}, {"result", "reason"}, "reader_review." + name):
                    if item["result"] not in ("pass", "fail", "pending") or not _text(item["reason"]):
                        self.issue("invalid_reader_review", name + " needs a valid result and a specific reason", True)
                    elif item["result"] != "pass":
                        self.issue("reader_review_incomplete", name + ": " + item["result"])
        self.fact_review(r["fact_review"])

    def fact_review(self, review):
        required = {"coverage", "reason", "docs_checked_on", "change_review", "claims", "unused_facts"}
        if not self.fields(review, required, required, "fact_review"):
            return
        if review["coverage"] not in ("reviewed", "pending") or not _text(review["reason"]):
            self.issue("invalid_coverage", "coverage needs reviewed/pending and a full-manuscript review reason", True)
        elif review["coverage"] != "reviewed":
            self.issue("coverage_pending", "full-manuscript claim coverage has not been reviewed")
        checked = self.checked_date(review["docs_checked_on"], "docs_checked_on", required=False)
        self.fresh(checked, "docs_checked_on")
        update = review["change_review"]
        fields = {"url", "locator", "checked_on", "effect"}
        if self.fields(update, fields, fields, "change_review"):
            if update["url"] == "":
                self.issue("update_pending", "official update/maturity source has not been read")
            elif not _official_url(update["url"], OPENAI_HOSTS, True):
                self.issue("unofficial_update_url", "update source must be an official OpenAI page or openai/codex repository", True)
            for key in ("locator", "effect"):
                if not _text(update[key]):
                    self.issue("update_pending", "change_review." + key + " must describe the actual reading")
            changed = self.checked_date(update["checked_on"], "change_review.checked_on", required=False)
            self.fresh(changed, "change_review.checked_on")
        unit = self.context.units[self.uid]
        refs = set(unit.get("facts", []))
        claims, unused = review["claims"], review["unused_facts"]
        if not isinstance(claims, list):
            self.issue("invalid_claims", "claims must be a list", True)
            claims = []
        if not isinstance(unused, dict):
            self.issue("invalid_unused_facts", "unused_facts must map registered fact IDs to reasons", True)
            unused = {}
        for fid, reason in unused.items():
            if fid not in refs or not _text(reason):
                self.issue("invalid_unused_facts", "unused fact must belong to this unit and have a reason: " + str(fid), True)
        text = _safe(self.context.root, unit["path"]).read_text(encoding="utf-8")
        covered, seen = set(), set()
        for index, claim in enumerate(claims):
            fid = self.claim(claim, index, refs, text, seen)
            if fid:
                covered.add(fid)
        overlap = covered.intersection(unused)
        if overlap:
            self.issue("contradictory_coverage", "facts cannot be both used and unused: " + ", ".join(sorted(overlap)), True)
        if review["coverage"] == "reviewed":
            missing = refs - covered - set(unused)
            if missing:
                self.issue("missing_fact_coverage", "referenced facts lack reviewed claims or unused reasons: " + ", ".join(sorted(missing)))

    def claim(self, claim, index, refs, manuscript, seen):
        fields = {"anchor", "fact_id", "url", "locator", "checked_on", "surface", "platforms", "availability", "conditions", "support", "rationale", "notice"}
        label = "claims[" + str(index) + "]"
        if not self.fields(claim, fields, fields, label):
            return None
        fid = claim["fact_id"]
        if not isinstance(fid, str) or fid not in refs:
            self.issue("unknown_claim_fact", label + " fact_id is not in this unit's book.yaml facts", True)
            return None
        fact = self.context.facts[fid]
        non_text = [key for key in fields - {"checked_on", "platforms"} if not isinstance(claim[key], str)]
        if non_text:
            self.issue("invalid_claim_schema", label + " fields must be text: " + ", ".join(sorted(non_text)), True)
            return fid
        anchor = claim["anchor"]
        if not _text(anchor) or manuscript.count(anchor) != 1:
            self.issue("invalid_anchor", label + " anchor must occur exactly once in the chapter", True)
        else:
            pair = (fid, anchor)
            if pair in seen:
                self.issue("duplicate_claim", label + " duplicates a fact/anchor pair", True)
            seen.add(pair)
        for field in ("locator", "conditions", "rationale"):
            if not _text(claim[field]):
                self.issue("incomplete_claim", label + "." + field + " needs an explicit explanation")
        checked = self.checked_date(claim["checked_on"], label + ".checked_on", required=False)
        self.fresh(checked, label + ".checked_on")
        if claim["surface"] not in SURFACES or claim["availability"] not in AVAILABILITIES:
            self.issue("invalid_claim_scope", label + " needs a valid surface and availability", True)
        try:
            platforms = set(_strings(claim["platforms"], label + ".platforms"))
            if not platforms or not platforms.issubset(self.context.platforms):
                raise GateError("claim platforms must be nonempty and within the book's promised platforms")
            hosts, github, surfaces, vendor_platforms = _authority(fact)
            if not _official_url(claim["url"], hosts, github):
                self.issue("unofficial_claim_url", label + " source is outside the fact's official authority", True)
            if claim["surface"] not in surfaces:
                self.issue("scope_mismatch", label + " surface conflicts with the fact product", True)
            if vendor_platforms and not platforms.issubset(vendor_platforms):
                self.issue("scope_mismatch", label + " platform conflicts with the OS vendor fact", True)
            scope = fact["scope"]
            if "surface" in scope and scope["surface"] != claim["surface"]:
                self.issue("scope_mismatch", label + " surface conflicts with facts.yaml scope", True)
            if "platforms" in scope and not platforms.issubset(_strings(scope["platforms"], fid + ".scope.platforms")):
                self.issue("scope_mismatch", label + " platforms conflict with facts.yaml scope", True)
            if "availability" in scope and scope["availability"] != claim["availability"]:
                self.issue("scope_mismatch", label + " availability conflicts with facts.yaml scope", True)
        except GateError as exc:
            self.issue("invalid_claim_scope", label + ": " + str(exc), True)
        sources = fact.get("sources")
        urls = {s.get("url") for s in sources if isinstance(s, dict) and isinstance(s.get("url"), str)} if isinstance(sources, list) else set()
        if claim["url"] not in urls:
            self.issue("unregistered_source", label + " url is not registered in this fact's sources", True)
        if fact.get("status") != "confirmed":
            self.issue("fact_not_confirmed", label + " canonical fact is not confirmed")
        if claim["support"] not in {"supported", "partial", "conflict", "unverified"}:
            self.issue("invalid_support", label + " has invalid support state", True)
        elif claim["support"] != "supported":
            self.issue("unsupported_claim", label + ": " + claim["support"])
        if not isinstance(claim["notice"], str):
            self.issue("invalid_notice", label + " notice must be text (empty for stable if unnecessary)", True)
        elif claim["availability"] in {"beta", "experimental", "deprecated"} and (not _text(claim["notice"]) or claim["notice"] not in manuscript):
            self.issue("missing_availability_notice", label + " beta/experimental/deprecated availability needs an actual notice in the chapter")
        # Unspecified maturity stays in the private/editorial conditions and
        # rationale; it does not force repetitive caveats into reader prose.
        # The reviewer must not infer stable merely from a new page or client.
        return fid


def validate(root, units=None, publication=False, today=None):
    """Validate exact-input review records; no output implies semantic proof.

    Structural errors always block. Missing, pending, unsupported or stale reviews
    warn during drafting and block publication. Records never replace GUI tests.
    """
    result = {"errors": [], "warnings": []}
    try:
        context, day = Context(root), _today(today)
        selected = list(context.units) if units is None else ([units] if isinstance(units, str) else list(units))
        _strings(selected, "selected units")
        if not selected or set(selected) - context.units.keys():
            raise GateError("select one or more known book unit IDs")
        for uid in selected:
            # Detect prerequisite cycles even before a review record exists.
            context.dependencies(uid)
            relative = "checks/writing/" + uid + ".yaml"
            path = context.root / relative
            if not path.exists():
                key = "errors" if publication else "warnings"
                result[key].append({"code": "missing_writing_review", "path": relative, "message": uid + " has no writing review; draft may continue, publication may not"})
                continue
            record = _load(_safe(context.root, relative))
            review = Review(context, uid, record, publication, day)
            review.run()
            result["errors"].extend(review.errors)
            result["warnings"].extend(review.warnings)
    except (GateError, OSError, UnicodeError, TypeError, KeyError) as exc:
        result["errors"].append({"code": "invalid_writing_gate_input", "path": "book.yaml / facts.yaml / checks/writing", "message": str(exc)})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="print a pending YAML review template; never write a receipt")
    prep.add_argument("--unit", required=True)
    prep.add_argument("--evidence-path", action="append", default=[])
    check = sub.add_parser("check", help="validate records without fetching or writing")
    check.add_argument("--units", nargs="+")
    check.add_argument("--publication", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        try:
            record = prepare(args.root, args.unit, evidence_paths=args.evidence_path)
            context = Context(args.root)
            dependencies = context.dependencies(args.unit)
            fact_ids = sorted({fid for uid in dependencies for fid in context.units[uid].get("facts", [])})
            inputs = {
                "unit": args.unit,
                "title": context.units[args.unit].get("title", args.unit),
                "path": context.units[args.unit]["path"],
                "audience": context.book.get("audience"),
                "platforms": context.book.get("platforms"),
                "baseline": context.book.get("baseline"),
                "prerequisites": [{"id": uid, "path": context.units[uid]["path"], "title": context.units[uid].get("title", uid)} for uid in dependencies if uid != args.unit],
                "facts": [{"id": fid, "claim": context.facts[fid]["claim"], "scope": context.facts[fid]["scope"], "urls": [source["url"] for source in context.facts[fid]["sources"]]} for fid in fact_ids],
                "rule_inputs": list(RULE_PATHS),
            }
            print("# 写前输入：以下是账本摘录，不是本轮官方核验结果。请读完整正文、前置和规则。")
            for line in yaml.safe_dump(inputs, allow_unicode=True, sort_keys=False).splitlines():
                print("# " + line)
            print("# 当批读取官方原页及更新；查阅日期须在 batch_started_on 与 reviewed_on 之间（含当天），正常跨日可复用。")
            print("# 未标注成熟度用 unspecified，在 conditions/rationale 留边界；不能据新页面或新版客户端填 stable。")
            print("# 下方只是 pending 模板；不证明实际阅读、语义支持、GUI 实测或真实读者试读。")
            print(yaml.safe_dump(record, allow_unicode=True, sort_keys=False), end="")
            return 0
        except (GateError, OSError, UnicodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    result = validate(args.root, args.units, args.publication)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
