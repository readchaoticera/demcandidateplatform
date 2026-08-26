#!/usr/bin/env python3
"""Derive the self-contained Artifact copy of the analysis page.

Usage:
    python scripts/build_artifact.py [--src docs/analysis.html]
                                     [--css docs/house.css]
                                     [--out results/2026-08-26/analysis.html]

The Pages copy links ``house.css``; an Artifact is published as a single file
behind a strict CSP, so the shared stylesheet is inlined here instead of being
maintained twice. The document skeleton is also stripped, because the Artifact
runtime supplies its own, and the back-link is dropped since the Artifact has
no sibling page to return to.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def build(src: Path, css: Path) -> str:
    html = src.read_text(encoding="utf-8")
    sheet = css.read_text(encoding="utf-8")

    title = re.search(r"<title>.*?</title>", html, re.S)
    if not title:
        raise SystemExit(f"{src}: no <title> found")

    page_css = re.search(r"<style>(.*?)</style>", html, re.S)
    if not page_css:
        raise SystemExit(f"{src}: no <style> block found")

    body = re.search(r"<body>(.*)</body>", html, re.S)
    if not body:
        raise SystemExit(f"{src}: no <body> found")

    content = re.sub(r'\s*<a class="backlink".*?</a>\s*', "\n\n  ", body.group(1), flags=re.S)

    return (
        f"{title.group(0)}\n"
        "<style>\n"
        "/* house.css inlined: an Artifact is a single file behind a strict CSP.\n"
        "   Edit docs/house.css and re-run scripts/build_artifact.py. */\n"
        f"{sheet.strip()}\n\n"
        "/* ---- page ---- */\n"
        f"{page_css.group(1).strip()}\n"
        "</style>\n"
        f"{content.strip()}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=Path("docs/analysis.html"))
    ap.add_argument("--css", type=Path, default=Path("docs/house.css"))
    ap.add_argument("--out", type=Path, default=Path("results/2026-08-26/analysis.html"))
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(args.src, args.css), encoding="utf-8")
    print(f"wrote {args.out}: {args.out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
