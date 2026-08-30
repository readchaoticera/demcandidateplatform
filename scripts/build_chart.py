#!/usr/bin/env python3
"""Render the newsletter chart: Medicare for All support by Cook rating.

    python3 scripts/build_chart.py [--data docs/data.json]
                                   [--out results/2026-08-26/charts/m4a_by_cook_rating.png]

Reads the published dashboard data so the chart cannot drift from the site, and
renders through headless Chromium so the newsletter's own faces - IBM Plex Serif
Bold for the title, Inter for everything else - are the ones that come out.

Fonts are fetched from Google Fonts on first run and cached under data/cache;
both families are OFL-licensed. The output is 2x for print and retina.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

FONT_CACHE = Path("data/cache/fonts")
FACES = {
    "plexserif700.ttf": ("IBM Plex Serif", "700"),
    "inter400.ttf": ("Inter", "400"),
    "inter600.ttf": ("Inter", "600"),
}
#: Bars are scaled against this rather than 100%, so the field's actual range
#: fills the width instead of hugging the axis.
MAX_PCT = 65.0
BAR_ZONE_PX = 830
ACCENT = "#419eff"


def _fetch_fonts() -> dict[str, str]:
    """Return {filename: base64}, downloading anything not already cached."""
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    # An old user-agent makes Google Fonts serve TrueType rather than woff2,
    # which Chromium takes from a data: URI without further ceremony.
    ua = "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)"
    out: dict[str, str] = {}
    for name, (family, weight) in FACES.items():
        path = FONT_CACHE / name
        if not path.exists():
            css_url = (f"https://fonts.googleapis.com/css?family="
                       f"{family.replace(' ', '+')}:{weight}")
            css = subprocess.run(["curl", "-sS", "-m", "40", "-A", ua, css_url],
                                 capture_output=True, text=True, check=True).stdout
            match = re.search(r"url\(([^)]+)\)", css)
            if not match:
                raise SystemExit(f"no font URL for {family} {weight}; is the network up?")
            subprocess.run(["curl", "-sS", "-m", "60", "-o", str(path), match.group(1)],
                           check=True)
        out[name] = base64.b64encode(path.read_bytes()).decode()
    return out


def tally(data: dict) -> list[tuple[str, int, int]]:
    """[(rating, democrats, supporters)] in Cook's own order.

    Rows with no rating are dropped rather than bucketed: the only one is the
    D.C. delegate seat, which Cook does not rate, and a single-candidate group
    would read as a 100% bar.
    """
    totals: dict[str, list[int]] = {}
    for cand in data["candidates"]:
        if not cand.get("cr"):
            continue
        row = totals.setdefault(cand["cr"], [0, 0])
        row[0] += 1
        row[1] += cand["bk"] == "supports_m4a"
    return [(r, *totals[r]) for r in data["rating_order"] if r in totals]


def render_html(rows: list[tuple[str, int, int]], fonts: dict[str, str],
                as_of: str, ratings_as_of: str) -> str:
    bars = []
    for label, n, sup in rows:
        pct = 100 * sup / n
        # A true zero draws no bar. A hairline stub would read as a small value.
        width = round(pct / MAX_PCT * BAR_ZONE_PX)
        bars.append(
            f'<div class="row"><div class="cat">{label}'
            f'<span class="n">{n} Dem{"s" if n != 1 else ""}</span></div>'
            f'<div class="bar" style="width:{width}px"></div>'
            f'<div class="val"><b>{pct:.1f}%</b>'
            f'<span class="of">{sup} of {n}</span></div></div>'
        )
    small = [f"{r} ({n})" for r, n, _ in sorted(rows, key=lambda x: x[1]) if n <= 20]
    small_note = (
        f"{'Four' if len(small) == 4 else str(len(small))} groups are small &mdash; "
        + ", ".join(small[:-1]) + f" and {small[-1]}" +
        " &mdash; so read those shares with their denominators. "
    ) if len(small) > 1 else ""

    face = lambda fam, wt, key: (
        f"@font-face{{font-family:'{fam}';font-weight:{wt};"
        f"src:url(data:font/ttf;base64,{fonts[key]}) format('truetype');}}"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
{face('Plex', 700, 'plexserif700.ttf')}
{face('Inter', 400, 'inter400.ttf')}
{face('Inter', 600, 'inter600.ttf')}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:1520px;background:#fff;font-family:'Inter',sans-serif;color:#111418;
     padding:52px 60px 34px;-webkit-font-smoothing:antialiased}}
h1{{font-family:'Plex',serif;font-weight:700;font-size:43px;letter-spacing:-.4px;line-height:1.12}}
.sub{{font-size:19.5px;color:#5b6675;margin-top:13px;line-height:1.5;max-width:1260px}}
.chart{{margin-top:40px;border-left:2px solid #e6ecf3;margin-left:186px}}
.row{{display:flex;align-items:center;height:80px;margin-left:-186px}}
.cat{{width:186px;text-align:right;padding-right:22px;font-size:22px;font-weight:600;flex:none}}
.cat .n{{display:block;font-size:15px;font-weight:400;color:#8792a2;margin-top:3px}}
.bar{{height:44px;background:{ACCENT};border-radius:0 5px 5px 0;flex:none}}
.val{{display:flex;align-items:baseline;gap:11px;padding-left:16px;white-space:nowrap}}
.val b{{font-size:26px;font-weight:600}}
.val .of{{font-size:16.5px;color:#8792a2}}
.foot{{margin-top:30px;padding-top:18px;border-top:1px solid #e6ecf3;
      font-size:15.5px;color:#5b6675;line-height:1.55}}
</style></head><body>
<h1>Democratic Support for Medicare for All, by District Competitiveness</h1>
<p class="sub">Share of Democratic candidates on the November 2026 U.S. House ballot who back
Medicare for All or single-payer &mdash; counting cosponsors of H.R.3069 alongside those who say so
publicly &mdash; grouped by The Cook Political Report&rsquo;s rating of the seat.</p>
<div class="chart">{''.join(bars)}</div>
<p class="foot">Chart: Kyle Tharp &#124; Chaotic Era Newsletter &#124; Data: candidates&rsquo; own
campaign sites, the H.R.3069 cosponsor roll and news coverage, as of {as_of}.
Cook ratings as of {ratings_as_of}.<br>{small_note}Excludes the D.C. delegate seat, which Cook
does not rate.</p>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=Path("docs/data.json"))
    ap.add_argument("--out", type=Path,
                    default=Path("results/2026-08-26/charts/m4a_by_cook_rating.png"))
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args()

    data = json.loads(args.data.read_text(encoding="utf-8"))
    rows = tally(data)
    if not rows:
        print("no rated candidates in the data", file=sys.stderr)
        return 1

    ratings_as_of = data.get("rating_as_of", "")
    as_of = data.get("as_of", "")
    try:
        from datetime import date
        y, m, d = (int(p) for p in as_of.split("-"))
        as_of = date(y, m, d).strftime("%b. %-d, %Y")
    except Exception:
        pass

    html = render_html(rows, _fetch_fonts(), as_of, ratings_as_of)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".html")
    tmp.write_text(html, encoding="utf-8")

    from playwright.sync_api import sync_playwright
    exe = next((str(p) for p in Path("/opt/pw-browsers").glob("*/chrome-linux/chrome")), None)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**({"executable_path": exe} if exe else {}))
        page = browser.new_page(viewport={"width": 1520, "height": 900},
                                device_scale_factor=args.scale)
        page.goto(tmp.resolve().as_uri(), wait_until="networkidle")
        page.wait_for_timeout(600)
        page.set_viewport_size({"width": 1520, "height": page.evaluate("document.body.scrollHeight")})
        page.wait_for_timeout(200)
        page.screenshot(path=str(args.out))
        browser.close()
    tmp.unlink()

    total = sum(n for _, n, _ in rows)
    sup = sum(s for _, _, s in rows)
    print(f"wrote {args.out}: {len(rows)} groups, {sup} of {total} rated candidates "
          f"({100*sup/total:.1f}%), {args.out.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
