#!/usr/bin/env python3
"""Check an explicit delivery scope. No build, upload, or publication."""
import os
from pathlib import Path
import subprocess
import sys
import yaml


def delivery_units(raw, book):
    units = raw.split()
    registered = [unit["id"] for unit in book["units"]]
    if units == ["all"]:
        return registered
    if not units or len(units) != len(set(units)) or any(unit not in registered for unit in units):
        raise ValueError("Specify known unit IDs once each, or exactly all; blank scope is not allowed")
    return units


def main():
    root = Path(__file__).resolve().parents[1]
    try:
        book = yaml.safe_load((root / "book.yaml").read_text(encoding="utf-8"))
        units = delivery_units(os.environ.get("DELIVERY_UNITS", ""), book)
    except (ValueError, OSError, KeyError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return subprocess.call([sys.executable, str(root / "tools/studio.py"),
                            "--root", str(root), "--json", "check", "--publication", "--units", *units])


if __name__ == "__main__":
    raise SystemExit(main())
