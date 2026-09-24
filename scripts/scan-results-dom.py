#!/usr/bin/env python3
"""
DOM scan of the PokerBet results page (directive 2026-09-24).

The user's correction: results come from the DOM at
    https://www.pokerbet.co.za/en/sports/results?game={GID}
Inner-text parsing proved insufficient — this script captures, for a given
game id:

  1. every network response that looks like data (XHR/fetch/JSON), with its
     URL, status, content-type and a size-capped body, so the authoritative
     record (and any game-id field) is visible;
  2. the scoreboard region of the DOM as STRUCTURED data — element tag,
     class list, data-* attributes and text — for every element whose text
     looks like a score, a team name, a date/time or a status label;
  3. whether the requested game id appears ANYWHERE in the DOM or in any
     response body (the identity proof the reconciler currently lacks).

READ-ONLY: it only reads a public web page.  Nothing local is written
except the optional --out JSON.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

URL = "https://www.pokerbet.co.za/en/sports/results?game={gid}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DATAISH = re.compile(r"json|javascript|text/plain", re.I)
INTERESTING = re.compile(
    r"result|event|game|match|score|fixture|history|sport", re.I)
DATEY = re.compile(r"\d{1,2}[:.]\d{2}|\d{4}-\d{2}-\d{2}")
SCOREY = re.compile(r"^\s*\d{1,3}\s*(?::|-|–)\s*\d{1,3}\s*$|^\s*\d{1,3}\s*$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gid", nargs="?", default="30840226")
    ap.add_argument("--wait", type=float, default=9.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dump-html", default=None)
    args = ap.parse_args()
    gid = str(args.gid)

    from playwright.sync_api import sync_playwright

    report: dict = {"game_id": gid, "url": URL.format(gid=gid),
                    "responses": [], "dom": {}, "html_hits": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  user_agent=UA, locale="en-ZA")
        page = ctx.new_page()

        def on_response(resp):
            try:
                ctype = (resp.headers or {}).get("content-type", "") or ""
                url = resp.url
                if not (DATAISH.search(ctype) or "/api" in url
                        or "graphql" in url.lower()):
                    return
                if "image" in ctype or "font" in ctype:
                    return
                body = resp.text()
            except Exception:
                return
            if len(body) > 400_000:
                body = body[:400_000] + "...[TRUNCATED]"
            report["responses"].append({
                "url": url, "status": resp.status, "type": ctype,
                "len": len(body), "gid_in_body": gid in body,
                "interesting_url": bool(INTERESTING.search(url)),
                "body": body,
            })

        page.on("response", on_response)
        page.goto(URL.format(gid=gid), timeout=45_000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(int(args.wait * 1000))
        # let late XHRs land
        page.wait_for_timeout(3000)

        html = page.content()
        report["html_len"] = len(html)
        report["html_hits"] = {
            "gid_raw": html.count(gid),
            "gid_in_any_attr": bool(re.search(
                r'(?:data|id|href|value|ng-reflect)[^>]{0,120}' + re.escape(gid),
                html)),
            "data_attributes_sample": sorted(set(re.findall(
                r'\bdata-[a-z0-9-]+(?==)', html)))[:40],
            "class_tokens_sample": sorted(set(re.findall(
                r'class="([^"]{0,200})"', html)))[:0],
        }
        # all class tokens, most-frequent first — finds the scoreboard block
        toks: dict[str, int] = {}
        for c in re.findall(r'class="([^"]{0,300})"', html):
            for t in c.split():
                toks[t] = toks.get(t, 0) + 1
        report["class_tokens_top"] = sorted(
            toks.items(), key=lambda kv: -kv[1])[:60]

        # structured scan of the scoreboard region: element + classes +
        # data-* attrs + text for every "interesting" leaf
        dom_rows = page.evaluate(
            """() => {
              const out = [];
              const walk = (el, depth) => {
                if (depth > 26) return;
                const t = (el.childElementCount === 0
                           ? (el.textContent || '') : '').trim();
                if (t) out.push({
                  tag: el.tagName.toLowerCase(),
                  cls: el.className && el.className.toString
                       ? el.className.toString() : '',
                  attrs: Object.fromEntries(
                    Array.from(el.attributes)
                      .filter(a => a.name.startsWith('data-')
                                || a.name === 'id' || a.name === 'href')
                      .map(a => [a.name, String(a.value).slice(0, 120)])),
                  text: t.slice(0, 120),
                });
                for (const c of el.children) walk(c, depth + 1);
              };
              walk(document.body, 0);
              return out;
            }""")
        scorey = [r for r in dom_rows
                  if SCOREY.match(r["text"]) or DATEY.search(r["text"])
                  or r["attrs"]]
        report["dom"] = {"leaf_count": len(dom_rows),
                         "scored_or_dated": scorey[:120]}
        report["dom_all_text_lines"] = [r["text"] for r in dom_rows][:200]

        if args.dump_html:
            Path(args.dump_html).write_text(html)
        # request URL echoed after any client-side redirect
        report["final_url"] = page.url
        browser.close()

    hit_bodies = [r for r in report["responses"] if r["gid_in_body"]]
    print(f"game_id            : {gid}")
    print(f"final url          : {report['final_url']}")
    print(f"dom size           : {report['html_len']} bytes, "
          f"{report['dom']['leaf_count']} text leaves")
    print(f"game id in DOM     : raw occurrences={report['html_hits']['gid_raw']}"
          f"  in an attribute={report['html_hits']['gid_in_any_attr']}")
    print(f"data-* attrs seen  : {report['html_hits']['data_attributes_sample']}")
    print(f"\nnetwork: {len(report['responses'])} data-ish responses; "
          f"{len(hit_bodies)} carry the game id")
    for r in report["responses"]:
        if r["interesting_url"] or r["gid_in_body"]:
            print(f"  [{r['status']}] {r['len']:>8}B gid={r['gid_in_body']} "
                  f"{r['url'][:150]}")
    print("\n--- scoreboard/DOM leaves with scores, dates or data attrs ---")
    for r in report["dom"]["scored_or_dated"][:60]:
        print(f"  <{r['tag']} class='{r['cls'][:70]}' {r['attrs']}> "
              f"{r['text'][:60]!r}")
    print("\n--- every text line ---")
    for t in report["dom_all_text_lines"][:80]:
        print(f"  {t!r}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
        print(f"\njson -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
