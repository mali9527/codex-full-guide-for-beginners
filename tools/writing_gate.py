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
import subprocess
from urllib.parse import unquote, urlsplit

import yaml

RULE_PATHS = (
    "写作约定.md", "术语翻译表.md", "案例设计.md", "写作核验机制.md",
)
READER_ITEMS = (
    "prior_knowledge", "concept_timing", "information_value", "natural_style",
    "localization", "independent_creation",
)
SURFACES = {"codex-desktop", "codex-cli", "os", "other"}
AVAILABILITIES = {"stable", "beta", "experimental", "deprecated", "unspecified"}
AUTHORITY_PATH = "tools/official-sources.yaml"
STUDY_MODES = {"new_draft", "revision", "retrospective"}
STUDY_FIELDS = {"mode", "prepared_on", "base_manuscript_sha256", "reader_goal", "reader_start",
                "explain_now", "official_readings", "teaching_sequence", "defer", "case_plan"}
RISK_TERMS = re.compile(r"默认|绝不|不会|总是|自动|始终|保证|一定|无需|无需批准|沙盒|权限|联网|\bdefault\b|\bnever\b|\balways\b|\bautomatically\b", re.I)
PRODUCT_TERMS = re.compile(r"Codex|ChatGPT|权限|沙盒|CLI|Windows|Mac|\bLocal\b", re.I)
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
# Only these unchanged legacy samples may honestly use retrospective migration.
# A new chapter or substantive revision must save a real before-writing plan.
MIGRATION_SNAPSHOTS = {'workspace-files': '8d41fbf3e9bb15133bedea035acff250f9239391e001ed96f8a9be567676ce1a', 'first-task': 'a50f9ce225800c546765711bfb22987e32ec78efa3e580387a16630be3b64d70'}
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


def _parsed_https(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        decoded = unquote(parsed.path)
        # Encoded separators or another encoding layer must not change the repository boundary.
        if "%" in decoded or "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
            return None
        return parsed, decoded
    except ValueError:
        return None


def _official_url(value, authority):
    parsed = _parsed_https(value)
    if parsed is None:
        return False
    url, path = parsed
    if url.hostname in authority["hosts"]:
        return True
    for repository in authority.get("repositories", []):
        base, prefix = _parsed_https(repository)
        if url.hostname == base.hostname and (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


def _authorities(root):
    value = _load(_safe(root, AUTHORITY_PATH))
    if not isinstance(value, dict) or set(value) != {"schema", "products"} or type(value["schema"]) is not int or value["schema"] != 1 or not isinstance(value["products"], dict):
        raise GateError("official-sources.yaml needs schema: 1 and a products mapping")
    products = value["products"]
    for product, item in products.items():
        if not isinstance(product, str) or not SLUG.fullmatch(product) or not isinstance(item, dict) or set(item) - {"hosts", "repositories", "surfaces", "platforms"} or not {"hosts", "surfaces"}.issubset(item):
            raise GateError("invalid official authority: " + str(product))
        hosts = _strings(item["hosts"], product + ".hosts")
        if any(not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}", host) or host == "github.com" for host in hosts):
            raise GateError("authority hosts must be exact domains; github.com requires a repository path")
        repos = _strings(item.get("repositories", []), product + ".repositories")
        for repo in repos:
            parsed = _parsed_https(repo)
            if parsed is None or parsed[0].hostname != "github.com" or parsed[0].query or parsed[0].fragment or not re.fullmatch(r"/[^/]+/[^/]+", parsed[1]):
                raise GateError("authority repositories must name an exact HTTPS GitHub owner/repository")
        if not hosts and not repos:
            raise GateError("authority needs at least one host or repository")
        surfaces = set(_strings(item["surfaces"], product + ".surfaces"))
        if not surfaces or not surfaces.issubset(SURFACES):
            raise GateError("invalid authority surfaces: " + product)
        if "platforms" in item and not set(_strings(item["platforms"], product + ".platforms")).issubset({"windows", "mac"}):
            raise GateError("authority platforms exceed this book's operating systems")
    if "codex" not in products:
        raise GateError("official sources must include the codex update authority")
    return products


def _authority(fact, authorities):
    product = fact["scope"].get("product")
    if product not in authorities:
        raise GateError("unsupported official-source product: " + str(product))
    return authorities[product]


def _digest_bytes(raw, suffix=".md"):
    if suffix.lower() in {".md", ".markdown"}:
        # Ignore editorial trailing whitespace while preserving Markdown hard breaks and fenced code.
        lines, fenced = [], False
        for line in raw.decode("utf-8").splitlines():
            if re.match(r"^\s*(```|~~~)", line):
                fenced = not fenced
            if not fenced:
                trimmed = line.rstrip(" \t")
                line = trimmed + ("  " if trimmed and line.endswith("  ") else "")
            lines.append(line)
        raw = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _digest(path):
    return _digest_bytes(path.read_bytes(), path.suffix)


def _git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise GateError("Git evidence is unavailable for " + args[0])
    return result.stdout


def risk_candidates(manuscript):
    """A deliberately non-exhaustive reading aid, not an assertion detector."""
    return [{"line": number, "text": line.strip(), "signals": sorted(set(RISK_TERMS.findall(line)))}
            for number, line in enumerate(manuscript.splitlines(), 1)
            if line.strip() and not line.lstrip().startswith("#") and RISK_TERMS.search(line)]


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
        self.authorities = _authorities(self.root)
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
            "schema": 2,
            "unit": uid,
            "book": {k: self.book.get(k) for k in ("id", "product", "language", "audience", "platforms")},
            "units": {d: self.units[d] for d in deps},
            "facts": {fid: self.facts[fid] for fid in fact_ids},
            "files": {p: _digest(_safe(self.root, p)) for p in sorted(paths)},
            "authorities": {product: self.authorities.get(product) for product in sorted({"codex"} | {self.facts[fid]["scope"]["product"] for fid in fact_ids})},
            "evidence_paths": sorted(evidence_paths),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def fingerprint(root, unit, evidence_paths=None):
    """Return the fingerprint; this does not certify that inputs were reviewed."""
    return Context(root).fingerprint(unit, evidence_paths)


def prepare(root, unit, today=None, evidence_paths=None, mode="new_draft"):
    """Return an explicitly pending review template, never a pass receipt."""
    context = Context(root)
    day = _today(today).isoformat()
    if mode not in STUDY_MODES:
        raise GateError("unknown study mode")
    record = {
        "schema": 2, "unit": unit, "input_sha256": context.fingerprint(unit, evidence_paths),
        "evidence_paths": list(evidence_paths or []), "batch_started_on": day,
        "reviewed_on": day, "reviewer": "codex",
        "study_brief": {"mode": mode, "prepared_on": day,
                        "base_manuscript_sha256": _digest(_safe(context.root, context.units[unit]["path"])),
                        "reader_goal": "", "reader_start": [], "explain_now": [], "official_readings": [],
                        "teaching_sequence": [], "defer": [], "case_plan": ""},
        "study_commit": None,
        "study_review": {"result": "pending", "reason": "待比较官方理解、读者起点与本章讲法；不以链接存在证明已学习"},
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
        self.kind = None
        self.quality = {"editorial": "pass", "facts": "pass"}
        self.reused = set()

    def issue(self, code, message, structural=False, state="unknown"):
        target = self.errors if structural or self.publication else self.warnings
        target.append({"code": code, "path": self.path, "message": message})
        if code == "stale_input":
            state = "stale"
        ranks = {"pass": 0, "unknown": 1, "fail": 2, "stale": 3}
        for kind in ([self.kind] if self.kind else self.quality):
            if ranks[state] > ranks[self.quality[kind]]:
                self.quality[kind] = state

    def notice(self, code, message):
        self.warnings.append({"code": code, "path": self.path, "message": message})


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

    def fresh(self, checked, label, url=None, allow_reuse=True):
        if checked is None:
            return
        reused = allow_reuse and url is not None and (url, checked.isoformat()) in self.reused
        if self.batch_started and checked < self.batch_started and not reused:
            self.issue("documents_before_batch", label + " predates this batch without a matching reading_reuse declaration")
        if reused and (self.today - checked).days > MAX_PUBLICATION_AGE_DAYS:
            self.issue("documents_expired", label + " reused reading exceeds the 7-day reuse window")
        if self.reviewed and checked > self.reviewed:
            self.issue("documents_after_review", label + " postdates the completed review", True)
        if self.publication and (self.today - checked).days > MAX_PUBLICATION_AGE_DAYS:
            self.issue("documents_expired", label + " exceeds the book's 7-day publication window")

    def study(self):
        brief = self.record.get("study_brief")
        if not isinstance(brief, dict) or set(brief) != STUDY_FIELDS:
            self.issue("study_brief_pending", "schema 2 requires a complete study_brief skeleton before drafting")
            return
        if brief["mode"] not in STUDY_MODES:
            self.issue("invalid_study_mode", "study_brief.mode must be new_draft, revision or retrospective", True)
        prepared = self.checked_date(brief["prepared_on"], "study_brief.prepared_on")
        if prepared and self.reviewed and prepared > self.reviewed:
            self.issue("study_after_review", "the study brief cannot postdate its completed review", True)
        digest = brief["base_manuscript_sha256"]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            self.issue("invalid_study_base", "base_manuscript_sha256 must be a full SHA-256", True)
        for field in ("reader_goal", "case_plan"):
            if not _text(brief[field]):
                self.issue("study_brief_pending", "study_brief." + field + " must explain this chapter's own plan")
        for field in ("reader_start", "explain_now", "teaching_sequence", "defer"):
            try:
                values = _strings(brief[field], "study_brief." + field)
                if field in {"reader_start", "teaching_sequence"} and not values:
                    self.issue("study_brief_pending", "study_brief." + field + " cannot be empty")
            except GateError as exc:
                self.issue("invalid_study_brief", str(exc), True)
        readings = brief["official_readings"]
        if not isinstance(readings, list):
            self.issue("invalid_study_readings", "official_readings must be a list", True)
            readings = []
        refs = {fid for uid in self.context.dependencies(self.uid) for fid in self.context.units[uid].get("facts", [])}
        if refs and not readings:
            self.issue("study_readings_pending", "the chapter needs actual official study notes before its product explanation")
        for index, reading in enumerate(readings):
            fields = {"fact_id", "url", "locator", "checked_on", "understanding", "teaching_use"}
            label = "official_readings[" + str(index) + "]"
            if not self.fields(reading, fields, fields, label):
                continue
            if not all(_text(reading[field]) for field in fields - {"checked_on"}):
                self.issue("study_readings_pending", label + " needs the author's understanding and teaching use, not just a URL")
                continue
            fid = reading["fact_id"]
            if fid not in refs:
                self.issue("unknown_study_fact", label + " fact is not registered for this chapter or prerequisites", True)
                continue
            fact = self.context.facts[fid]
            try:
                if not _official_url(reading["url"], _authority(fact, self.context.authorities)):
                    self.issue("unofficial_study_url", label + " is outside the product authority", True)
            except GateError as exc:
                self.issue("invalid_study_authority", str(exc), True)
            if reading["url"] not in {source["url"] for source in fact["sources"]}:
                self.issue("unregistered_study_source", label + " source is not in facts.yaml", True)
            checked = self.checked_date(reading["checked_on"], label + ".checked_on")
            if checked and prepared and checked > prepared:
                self.issue("study_reading_after_plan", label + " was read after the declared plan; later readings belong in fact_review", True)
            elif checked and prepared and (prepared - checked).days > MAX_PUBLICATION_AGE_DAYS:
                self.issue("study_reading_expired", label + " was already older than seven days when the plan was prepared")
        item = self.record.get("study_review")
        if not isinstance(item, dict) or set(item) != {"result", "reason"}:
            self.issue("study_review_pending", "study_review needs result and reason")
        elif item["result"] not in {"pass", "fail", "pending"} or not _text(item["reason"]):
            self.issue("invalid_study_review", "study_review needs an honest result and concrete reasoning", True)
        elif item["result"] != "pass":
            self.issue("study_review_incomplete", "official study and teaching plan review is " + item["result"], state="fail" if item["result"] == "fail" else "unknown")
        commit = self.record.get("study_commit")
        if brief["mode"] == "new_draft" and _digest(_safe(self.context.root, self.context.units[self.uid]["path"])) == digest:
            self.issue("study_base_unchanged", "new_draft still matches its saved starting manuscript; complete the draft before claiming a writing review. A text change alone does not prove reading or thinking order.")
        if brief["mode"] == "retrospective":
            expected = MIGRATION_SNAPSHOTS.get(self.uid)
            current = _digest(_safe(self.context.root, self.context.units[self.uid]["path"]))
            if not expected or digest != expected or current != expected:
                self.issue("retrospective_not_eligible", "retrospective is reserved for the two unchanged legacy samples; new chapters and substantive edits require revision/new_draft with saved prior study")
            self.notice("retrospective_study", "This is retrospective calibration of existing prose; it does not prove official study preceded the original draft.")
        if not commit:
            if brief["mode"] != "retrospective":
                self.issue("study_commit_pending", "new drafts and revisions require a saved ancestor containing this plan and its unmodified base manuscript")
            return
        if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
            self.issue("invalid_study_commit", "study_commit must be a complete Git commit ID", True)
            return
        try:
            _git(self.context.root, "merge-base", "--is-ancestor", commit, "HEAD")
            saved = yaml.load(_git(self.context.root, "show", commit + ":" + self.path).decode("utf-8"), Loader=UniqueLoader)
            original = _git(self.context.root, "show", commit + ":" + self.context.units[self.uid]["path"])
            if not isinstance(saved, dict) or saved.get("study_brief") != brief:
                self.issue("study_plan_changed", "the saved ancestor does not contain this same study_brief")
            if _digest_bytes(original, Path(self.context.units[self.uid]["path"]).suffix) != digest:
                self.issue("study_base_changed", "the ancestor manuscript does not match the plan's recorded starting draft")
        except (GateError, UnicodeError, yaml.YAMLError) as exc:
            self.issue("study_commit_unavailable", "cannot verify the saved before-writing plan: " + str(exc) + ". Check that the original plan commit and files exist and are ancestors of HEAD. A shallow clone may need more history; squash or rebasing the plan may remove ancestry. Restore genuine saved history rather than replacing dates or inventing a new plan receipt.")

    def run(self):
        required = {"schema", "unit", "input_sha256", "batch_started_on", "reviewed_on", "reviewer", "reader_review", "fact_review"}
        if not self.fields(self.record, required | {"evidence_paths", "study_brief", "study_commit", "study_review"}, required, "review"):
            return
        r = self.record
        if type(r["schema"]) is int and r["schema"] == 1:
            self.issue("writing_schema_migration", "schema 1 is historical; migrate honestly to schema 2 before claiming a current review")
            return
        if type(r["schema"]) is not int or r["schema"] != 2 or r["unit"] != self.uid:
            self.issue("invalid_schema", "schema must be 2 and unit must match its filename", True)
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
        self.kind = "editorial"
        self.study()
        reader = r["reader_review"]
        if self.fields(reader, READER_ITEMS, READER_ITEMS, "reader_review"):
            for name in READER_ITEMS:
                item = reader[name]
                if self.fields(item, {"result", "reason"}, {"result", "reason"}, "reader_review." + name):
                    if item["result"] not in ("pass", "fail", "pending") or not _text(item["reason"]):
                        self.issue("invalid_reader_review", name + " needs a valid result and a specific reason", True)
                    elif item["result"] != "pass":
                        self.issue("reader_review_incomplete", name + ": " + item["result"], state="fail" if item["result"] == "fail" else "unknown")
        self.kind = "facts"
        self.fact_review(r["fact_review"])

    def fact_review(self, review):
        required = {"coverage", "reason", "docs_checked_on", "change_review", "claims", "unused_facts"}
        if not self.fields(review, required | {"reading_reuse"}, required, "fact_review"):
            return
        if review["coverage"] not in ("reviewed", "pending") or not _text(review["reason"]):
            self.issue("invalid_coverage", "coverage needs reviewed/pending and a full-manuscript review reason", True)
        elif review["coverage"] != "reviewed":
            self.issue("coverage_pending", "full-manuscript claim coverage has not been reviewed")
        reuse = review.get("reading_reuse", [])
        if not isinstance(reuse, list):
            self.issue("invalid_reading_reuse", "reading_reuse must be a list", True)
            reuse = []
        for index, item in enumerate(reuse):
            fields = {"url", "checked_on", "from_batch", "reason"}
            label = "reading_reuse[" + str(index) + "]"
            if not self.fields(item, fields, fields, label):
                continue
            if not all(_text(item[field]) for field in ("url", "from_batch", "reason")):
                self.issue("invalid_reading_reuse", label + " needs the original batch and a specific reuse reason", True)
                continue
            checked_reuse = self.checked_date(item["checked_on"], label + ".checked_on")
            registered = {source["url"] for fid in self.context.units[self.uid].get("facts", []) for source in self.context.facts[fid]["sources"]}
            if item["url"] not in registered:
                self.issue("invalid_reading_reuse", label + " must identify a registered chapter source", True)
            elif checked_reuse:
                self.reused.add((item["url"], checked_reuse.isoformat()))
        checked = self.checked_date(review["docs_checked_on"], "docs_checked_on", required=False)
        # Summary date may be the oldest actual reading; precise URL/date reuse is checked on every claim below.
        summary_reused = checked is not None and any(day == checked.isoformat() for _, day in self.reused)
        self.fresh(checked, "docs_checked_on", next((url for url, day in self.reused if checked and day == checked.isoformat()), None) if summary_reused else None)
        update = review["change_review"]
        fields = {"url", "locator", "checked_on", "effect"}
        if self.fields(update, fields, fields, "change_review"):
            if update["url"] == "":
                self.issue("update_pending", "official update/maturity source has not been read")
            elif not _official_url(update["url"], self.context.authorities["codex"]):
                self.issue("unofficial_update_url", "update source must be an official OpenAI page or openai/codex repository", True)
            for key in ("locator", "effect"):
                if not _text(update[key]):
                    self.issue("update_pending", "change_review." + key + " must describe the actual reading")
            changed = self.checked_date(update["checked_on"], "change_review.checked_on", required=False)
            self.fresh(changed, "change_review.checked_on", update["url"], allow_reuse=False)
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
        if refs and refs.issubset(unused) and not claims and any(PRODUCT_TERMS.search(candidate["text"]) for candidate in risk_candidates(text)):
            self.notice("unused_product_claims_review", "All registered facts are marked unused but the chapter contains product-risk wording; inspect the complete review packet. This heuristic does not decide semantic coverage.")
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
        if not _text(anchor) or manuscript.count(anchor) != 1 or anchor.strip().startswith("#") or any(re.sub(r"^\s*#{1,6}\s+", "", line).strip() == anchor.strip() for line in manuscript.splitlines() if line.lstrip().startswith("#")):
            self.issue("invalid_anchor", label + " anchor must identify actual prose exactly once, not only a heading", True)
        else:
            pair = (fid, anchor)
            if pair in seen:
                self.issue("duplicate_claim", label + " duplicates a fact/anchor pair", True)
            seen.add(pair)
        for field in ("locator", "conditions", "rationale"):
            if not _text(claim[field]):
                self.issue("incomplete_claim", label + "." + field + " needs an explicit explanation")
        checked = self.checked_date(claim["checked_on"], label + ".checked_on", required=False)
        self.fresh(checked, label + ".checked_on", claim["url"])
        if claim["surface"] not in SURFACES or claim["availability"] not in AVAILABILITIES:
            self.issue("invalid_claim_scope", label + " needs a valid surface and availability", True)
        try:
            platforms = set(_strings(claim["platforms"], label + ".platforms"))
            if not platforms or not platforms.issubset(self.context.platforms):
                raise GateError("claim platforms must be nonempty and within the book's promised platforms")
            authority = _authority(fact, self.context.authorities)
            if not _official_url(claim["url"], authority):
                self.issue("unofficial_claim_url", label + " source is outside the fact's official authority", True)
            if claim["surface"] not in authority["surfaces"]:
                self.issue("scope_mismatch", label + " surface conflicts with the fact product", True)
            if authority.get("platforms") and not platforms.issubset(authority["platforms"]):
                self.issue("scope_mismatch", label + " platform conflicts with the OS vendor fact", True)
            scope = fact["scope"]
            if "surface" in scope and scope["surface"] != claim["surface"]:
                self.issue("scope_mismatch", label + " surface conflicts with facts.yaml scope", True)
            if "surfaces" in scope and claim["surface"] not in _strings(scope["surfaces"], fid + ".scope.surfaces"):
                self.issue("scope_mismatch", label + " surface conflicts with facts.yaml surfaces", True)
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
            self.issue("unsupported_claim", label + ": " + claim["support"], state="fail" if claim["support"] in {"partial", "conflict"} else "unknown")
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
    result = {"errors": [], "warnings": [], "quality": {}}
    try:
        context, day = Context(root), _today(today)
        selected = list(context.units) if units is None else ([units] if isinstance(units, str) else list(units))
        _strings(selected, "selected units")
        if not selected or set(selected) - context.units.keys():
            raise GateError("select one or more known book unit IDs")
        result["quality"] = {uid: {"editorial": "unknown", "facts": "unknown"} for uid in selected}
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
            try:
                relevant = list(RULE_PATHS) + [AUTHORITY_PATH, "tools/writing_gate.py", "facts.yaml", "book.yaml"] + [context.units[dep]["path"] for dep in context.dependencies(uid)]
                if _git(context.root, "status", "--porcelain", "--", *relevant).strip():
                    review.notice("uncommitted_review_inputs", "Relevant inputs have uncommitted changes; this working-tree result must be checked again on the saved source commit.")
            except GateError:
                pass
            result["errors"].extend(review.errors)
            result["warnings"].extend(review.warnings)
            result["quality"][uid] = review.quality
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
    prep.add_argument("--mode", choices=sorted(STUDY_MODES), default="new_draft")
    packet = sub.add_parser("review-packet", help="print the entire manuscript and non-exhaustive review aids; never write or certify coverage")
    packet.add_argument("--unit", required=True)
    check = sub.add_parser("check", help="validate records without fetching or writing")
    check.add_argument("--units", nargs="+")
    check.add_argument("--publication", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        try:
            record = prepare(args.root, args.unit, evidence_paths=args.evidence_path, mode=args.mode)
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
            print("# 先完成 study_brief 并保存含原稿的提交，再起草；study_commit 指向该祖先。旧稿校准用 retrospective，不补造写前历史。")
            print("# 当批实际回查更新。旧读页仅以 reading_reuse 声明真实 URL/日期/原批次复用，不刷新读取日；最长7天。")
            try:
                relevant = list(RULE_PATHS) + [AUTHORITY_PATH, "tools/writing_gate.py", "facts.yaml", "book.yaml"] + [context.units[uid]["path"] for uid in dependencies]
                dirty = _git(context.root, "status", "--porcelain", "--", *relevant).decode("utf-8").strip()
                if dirty:
                    print("# 提醒：相关输入有未提交修改；当前指纹不代表已有固定提交，交付前必须在保存的提交上复核。")
            except GateError:
                print("# 当前没有可核对的 Git 基线；可以准备草稿，不能据此证明写前计划已保存。")
            print("# 未标注成熟度用 unspecified，在 conditions/rationale 留边界；不能据新页面或新版客户端填 stable。")
            print("# 下方只是 pending 模板；不证明实际阅读、语义支持、GUI 实测或真实读者试读。")
            print(yaml.safe_dump(record, allow_unicode=True, sort_keys=False), end="")
            return 0
        except (GateError, OSError, UnicodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "review-packet":
        try:
            context = Context(args.root)
            if args.unit not in context.units:
                raise GateError("unknown unit: " + args.unit)
            manuscript = _safe(context.root, context.units[args.unit]["path"]).read_text(encoding="utf-8")
            relative = "checks/writing/" + args.unit + ".yaml"
            record = _load(_safe(context.root, relative)) if (context.root / relative).exists() else {}
            print("# 完整正文审閱包：候选是非穷尽提示；未命中不证明无断言，不能据此自动填写 coverage 或 unused。")
            print("\n## 完整正文（含行号）\n")
            for number, line in enumerate(manuscript.splitlines(), 1):
                print(str(number) + " | " + line)
            print("\n## 已登记断言与官方来源\n")
            fact_review = record.get("fact_review", {}) if isinstance(record, dict) else {}
            print(yaml.safe_dump({"claims": fact_review.get("claims", []), "unused_facts": fact_review.get("unused_facts", {})}, allow_unicode=True, sort_keys=False))
            print("## 高风险表达候选（请求示例也可能命中，需人工分类）\n")
            print(yaml.safe_dump(risk_candidates(manuscript), allow_unicode=True, sort_keys=False))
            return 0
        except (GateError, OSError, UnicodeError, AttributeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    result = validate(args.root, args.units, args.publication)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
