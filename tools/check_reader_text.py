#!/usr/bin/env python3
"""Codex book-specific reader-text check; does not certify operational evidence."""
import argparse
import html
import json
from pathlib import Path
import re
import sys
import yaml

MARKERS = re.compile(
    r"编辑待(?:验证|核验|办|补充|实测)|编辑注[：:]|\b(?:PREP|RESULT)-\d{2,}\b"
    r"|<!--\s*(?:编辑|待验证|待核验|待实测|待补|TODO\b|FIXME\b|TBD\b)"
    r"|[\[【]\s*(?:待验证|待核验|待实测|待补图|补图占位)\s*[：:]",
    re.IGNORECASE,
)
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm"}


def findings(path):
    text = html.unescape(path.read_text(encoding="utf-8"))
    return sorted({text.count("\n", 0, m.start()) + 1 for m in MARKERS.finditer(text)})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("paths", nargs="*", type=Path,
                        help="Explicit reader text files/directories, relative to current directory")
    args = parser.parse_args(argv)
    try:
        if args.paths:
            targets = args.paths
        else:
            book = yaml.safe_load((args.root / "book.yaml").read_text(encoding="utf-8"))
            targets = [args.root / unit["path"] for unit in book["units"]]
            for key in ("combined", "readme"):
                if book.get("outputs", {}).get(key):
                    targets.append(args.root / book["outputs"][key])
        files = set()
        for path in targets:
            if not path.exists():
                raise ValueError("Missing reader target: " + str(path))
            if path.is_dir():
                children = {p.resolve() for p in path.rglob("*")
                            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES}
                if not children:
                    raise ValueError("No reader text files found: " + str(path))
                files.update(children)
            elif path.suffix.lower() in TEXT_SUFFIXES:
                files.add(path.resolve())
            else:
                raise ValueError("Use extracted text for this format: " + str(path))
        violations = []
        for path in sorted(files):
            for line in findings(path):
                try:
                    label = str(path.relative_to(args.root.resolve()))
                except ValueError:
                    label = str(path)
                violations.append({"path": label, "line": line})
        print(json.dumps({"ok": not violations, "files_checked": len(files),
                          "editorial_leftovers": violations}, ensure_ascii=False, indent=2))
        return 1 if violations else 0
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
