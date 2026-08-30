#!/usr/bin/env python3
"""Render the newsletter chart: Medicare for All support by Cook rating.

    python3 scripts/build_chart.py [--data docs/data.json]
                                   [--out results/2026-08-26/charts/m4a_by_cook_rating.png]

Reads the published dashboard data so the chart cannot drift from the site, and
renders through headless Chromium so the newsletter's own faces - IBM Plex Serif
Bold for the title, Inter for everything else - are the ones that come out.

Fonts are fetched from Google Fonts as woff2 on first run and cached under
data/cache; both families are OFL-licensed. The render asserts that the real
faces actually loaded, because a failed @font-face is invisible in the output -
it just quietly draws in a fallback serif. Output is 2x for print and retina.
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
    "plexserif700.woff2": ("IBM Plex Serif", "700"),
    "inter400.woff2": ("Inter", "400"),
    "inter600.woff2": ("Inter", "600"),
}
#: Google Fonts serves a format per user-agent, and gets it right only if you
#: tell the truth. An archaic UA returns EOT - Internet Explorer's format,
#: which Chromium cannot load at all, so every @font-face fails silently and
#: the page renders in fallback serif and sans that pass for the real thing at
#: a glance. A current Chrome UA returns woff2, which Chromium reads natively.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

#: First four bytes of a woff2 file. Checked on every fetch, because the
#: failure this guards against is invisible in the output.
WOFF2_MAGIC = b"wOF2"
#: Bars carry the raw count of supporters, scaled so the largest group fills
#: the zone. Counts, not shares: the chart answers "how many", and the field
#: sizes that would turn these into rates are given in the footnote instead.
BAR_ZONE_PX = 830

#: A diverging partisan ramp, not a categorical set: blue poles through a
#: neutral grey midpoint to red. Colour is redundant here - every bar is named
#: in the axis label beside it and carries its own number - so identity never
#: rests on hue, and the pale steps are legible because of the direct labels.
#: Lean D and Lean R are currently empty; their steps sit between the
#: neighbours so the ramp stays ordered if either ever fills.
RATING_COLORS = {
    "Solid D": "#419eff",
    "Likely D": "#9ec9f5",
    "Lean D": "#c9e0f8",
    "Toss Up": "#d9d9d9",
    "Lean R": "#ffd0dc",
    "Likely R": "#ffaabe",
    "Solid R": "#fa2c5d",
}
FALLBACK_COLOR = "#419eff"


#: Google Fonts and the ratings table both abbreviate months; the footnote
#: spells them out.
_MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
           "May": "May", "Jun": "June", "Jul": "July", "Aug": "August",
           "Sep": "September", "Sept": "September", "Oct": "October",
           "Nov": "November", "Dec": "December"}


def _long_date(text: str) -> str:
    """"Aug. 25, 2026" -> "August 25, 2026", leaving anything else alone."""
    match = re.match(r"([A-Z][a-z]{2,4})\.?\s+(\d{1,2},\s*\d{4})$", text.strip())
    if not match:
        return text
    return f"{_MONTHS.get(match.group(1), match.group(1))} {match.group(2)}"


def _latin_subset_url(css: str) -> str | None:
    """The woff2 URL for the Latin subset of a Google Fonts css2 response.

    css2 returns one @font-face per Unicode subset - Cyrillic, Greek,
    Vietnamese, Latin-Extended, Latin - and Latin is last, not first. Taking
    the first URL fetches a face that loads cleanly, satisfies
    document.fonts.check(), and contains no Latin glyphs at all, so the page
    renders in the fallback family while every signal says the font is fine.
    Pick the block whose range covers Basic Latin instead of trusting order.
    """
    for block in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
        rng = re.search(r"unicode-range:\s*([^;]+);", block)
        url = re.search(r"url\((https://[^)]+\.woff2)\)", block)
        if url and rng and "U+0000-00FF" in rng.group(1).replace(" ", ""):
            return url.group(1)
    # No subsetting at all (a single unranged face) is still fine.
    single = re.findall(r"@font-face\s*\{(.*?)\}", css, re.S)
    if len(single) == 1:
        url = re.search(r"url\((https://[^)]+\.woff2)\)", single[0])
        return url.group(1) if url else None
    return None


def _fetch_fonts() -> dict[str, str]:
    """Return {filename: base64}, downloading anything not already cached."""
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name, (family, weight) in FACES.items():
        path = FONT_CACHE / name
        if not path.exists() or path.read_bytes()[:4] != WOFF2_MAGIC:
            css_url = (f"https://fonts.googleapis.com/css2?family="
                       f"{family.replace(' ', '+')}:wght@{weight}")
            css = subprocess.run(["curl", "-sS", "-m", "40", "-A", BROWSER_UA, css_url],
                                 capture_output=True, text=True, check=True).stdout
            url = _latin_subset_url(css)
            if not url:
                raise SystemExit(f"no Latin woff2 for {family} {weight}; is the network up?")
            subprocess.run(["curl", "-sS", "-m", "60", "-A", BROWSER_UA,
                            "-o", str(path), url], check=True)
        blob = path.read_bytes()
        if blob[:4] != WOFF2_MAGIC:
            raise SystemExit(f"{path} is not woff2 (got {blob[:4]!r}); refusing to "
                             "render in a fallback face")
        out[name] = base64.b64encode(blob).decode()
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
    top = max(sup for _, _, sup in rows) or 1
    bars = []
    for label, n, sup in rows:
        # A true zero draws no bar. A hairline stub would read as a small count.
        width = round(sup / top * BAR_ZONE_PX)
        bars.append(
            f'<div class="row"><div class="cat">{label}</div>'
            f'<div class="bar" style="width:{width}px;'
            f'background:{RATING_COLORS.get(label, FALLBACK_COLOR)}"></div>'
            f'<div class="val"><b>{sup}</b></div></div>'
        )

    face = lambda fam, wt, key: (
        f"@font-face{{font-family:'{fam}';font-weight:{wt};"
        f"src:url(data:font/woff2;base64,{fonts[key]}) format('woff2');}}"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
{face('Plex', 700, 'plexserif700.woff2')}
{face('Inter', 400, 'inter400.woff2')}
{face('Inter', 600, 'inter600.woff2')}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:1520px;background:#fff;font-family:'Inter',sans-serif;color:#111418;
     padding:52px 60px 34px;-webkit-font-smoothing:antialiased}}
h1{{font-family:'Plex',serif;font-weight:700;font-size:44px;letter-spacing:-.45px;line-height:1.1}}
.sub{{font-size:22px;color:#5b6675;margin-top:15px;line-height:1.48;max-width:1400px}}
.chart{{margin-top:44px;border-left:2px solid #e6ecf3;margin-left:200px}}
.row{{display:flex;align-items:center;height:66px;margin-left:-186px}}
.cat{{width:200px;text-align:right;padding-right:24px;font-size:27px;font-weight:600;flex:none}}
.bar{{height:46px;border-radius:0 5px 5px 0;flex:none}}
.val{{padding-left:18px;white-space:nowrap}}
.val b{{font-size:33px;font-weight:600}}
.foot{{margin-top:34px;padding-top:20px;border-top:1px solid #e6ecf3;
      font-size:16.5px;color:#5b6675;line-height:1.55}}
</style></head><body>
<h1>Democrats Backing Medicare for All, by District Competitiveness</h1>
<p class="sub">Number of Democratic candidates on the November 2026 U.S. House ballot who back
Medicare for All or single-payer &mdash; counting cosponsors of H.R.3069 alongside those who say so
publicly &mdash; grouped by The Cook Political Report&rsquo;s rating of the seat.</p>
<div class="chart">{''.join(bars)}</div>
<p class="foot">Chart: Kyle Tharp &#124; Chaotic Era Newsletter. Data: campaign sites,
H.R. 3069, online issue indexes, public news reports. Cook ratings as of {ratings_as_of}.</p>
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

    ratings_as_of = _long_date(data.get("rating_as_of", ""))
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

        # A failed @font-face does not error - it silently draws in a fallback
        # face that passes for the real thing at a glance, which is exactly how
        # this shipped in EOT for several revisions. Ask the page directly, and
        # confirm the title is measurably not the generic serif.
        loaded = page.evaluate("""() => {
            // A long probe string so the width gap is far larger than any
            // sub-pixel noise: Inter SemiBold and the fallback sans are close
            // enough at one line that a short sample proves little.
            const SAMPLE = ('Democrats Backing Medicare for All 107 Solid D ').repeat(6);
            const probe = (font) => {
                const s = document.createElement('span');
                s.textContent = SAMPLE;
                s.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;font:' + font;
                document.body.appendChild(s);
                const w = s.getBoundingClientRect().width;
                s.remove();
                return w;
            };
            // Each face is measured against its own generic fallback. Equal
            // widths mean the named family never took effect - which is what a
            // face that loads but carries no Latin glyph looks like.
            const faces = [
                ["IBM Plex Serif Bold", "700 44px 'Plex',serif",  "700 44px serif"],
                ["Inter Regular",       "400 22px Inter,sans-serif", "400 22px sans-serif"],
                ["Inter SemiBold 27",   "600 27px Inter,sans-serif", "600 27px sans-serif"],
                ["Inter SemiBold 33",   "600 33px Inter,sans-serif", "600 33px sans-serif"],
            ].map(([name, real, fb]) => ({name, real: probe(real), fb: probe(fb)}));

            const declared = [["700 44px Plex", 'Plex'], ["400 22px Inter", 'Inter'],
                              ["600 27px Inter", 'Inter']];
            const missing = declared.filter(([f]) => !document.fonts.check(f)).map(([,n]) => n);
            // Every visible element should be asking for one of the two families.
            const used = {};
            for (const [name, sel] of [["title","h1"], ["subtitle",".sub"], ["category",".cat"],
                                       ["value",".val b"], ["footer",".foot"]])
                used[name] = getComputedStyle(document.querySelector(sel)).fontFamily;

            const h1 = document.querySelector('h1');
            const lines = Math.round(h1.getBoundingClientRect().height
                          / parseFloat(getComputedStyle(h1).lineHeight));
            return {missing, faces, used, lines};
        }""")
        if loaded["missing"]:
            raise SystemExit("font(s) failed to load: " + ", ".join(sorted(set(loaded["missing"]))))
        for face in loaded["faces"]:
            if abs(face["real"] - face["fb"]) < 2.0:
                raise SystemExit(
                    f"{face['name']} measures the same as its generic fallback "
                    f"({face['real']:.1f}px vs {face['fb']:.1f}px); the face did not take "
                    "effect - most likely the wrong Unicode subset was fetched")
        for name, family in loaded["used"].items():
            head = family.split(",")[0].strip().strip("'\"")
            if head not in ("Plex", "Inter"):
                raise SystemExit(f"the {name} asks for {family!r}, not Plex or Inter")
        if loaded["lines"] > 1:
            print(f"note: the title wraps to {loaded['lines']} lines at this size",
                  file=sys.stderr)
        print("fonts ok: " + " | ".join(
            f"{f['name']} {f['real']:.0f}px vs fallback {f['fb']:.0f}px"
            for f in loaded["faces"]))
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
