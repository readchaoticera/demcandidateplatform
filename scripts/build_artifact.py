#!/usr/bin/env python3
"""Derive the self-contained Artifact copy of the analysis page.

Usage:
    python scripts/build_artifact.py [--src docs/analysis.html]
                                     [--css docs/house.css]
                                     [--out results/2026-08-26/analysis.html]

The Pages copy links ``styles.css``; an Artifact is published as a single file
behind a strict CSP, so the shared stylesheet is inlined here instead of being
maintained twice. The document skeleton is also stripped, because the Artifact
runtime supplies its own, and the back-link is dropped since the Artifact has
no sibling page to return to.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def build(src: Path, css: Path, logo: Path | None = None) -> str:
    html = src.read_text(encoding="utf-8")
    sheet = css.read_text(encoding="utf-8")

    links = "\n".join(re.findall(r'<link[^>]+fonts\.(?:googleapis|gstatic)\.com[^>]*>', html, re.S)
                      + re.findall(r'<link[^>]+rel="preconnect"[^>]*>', html, re.S))

    title = re.search(r"<title>.*?</title>", html, re.S)
    if not title:
        raise SystemExit(f"{src}: no <title> found")

    page_css = re.search(r"<style>(.*?)</style>", html, re.S)
    if not page_css:
        raise SystemExit(f"{src}: no <style> block found")

    body = re.search(r"<body>(.*)</body>", html, re.S)
    if not body:
        raise SystemExit(f"{src}: no <body> found")

    content = body.group(1)
    # No sibling page inside an Artifact, so the cross-links go.
    content = re.sub(r'\s*<p class="kicker"><a class="backlink".*?</p>', "", content, flags=re.S)
    content = re.sub(r'\s*<a href="\./">[^<]*</a>', "", content, flags=re.S)

    # An Artifact is a single file behind a CSP that blocks external images, so
    # the masthead logo is inlined rather than fetched.
    if logo and logo.exists():
        import base64
        uri = "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode("ascii")
        content = content.replace('src="assets/logo.png"', f'src="{uri}"')

    return (
        f"{title.group(0)}\n"
        f"{links}\n"
        "<style>\n"
        "/* styles.css inlined: an Artifact is a single file behind a strict CSP.\n"
        "   Edit docs/styles.css and re-run scripts/build_artifact.py. */\n"
        f"{sheet.strip()}\n\n"
        "/* ---- page ---- */\n"
        f"{page_css.group(1).strip()}\n"
        "</style>\n"
        f"{content.strip()}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=Path("docs/analysis.html"))
    ap.add_argument("--css", type=Path, default=Path("docs/styles.css"))
    ap.add_argument("--logo", type=Path, default=Path("docs/assets/logo.png"))
    ap.add_argument("--out", type=Path, default=Path("results/2026-08-26/analysis.html"))
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(args.src, args.css, args.logo), encoding="utf-8")
    print(f"wrote {args.out}: {args.out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
