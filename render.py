#!/usr/bin/env python3
"""Render a pulse run as the page at mikyo.one/openrouter/.

Usage: python3 render.py [--run runs/<date>] [--repo https://github.com/<owner>/<repo>] [--out path/to/index.html]
Reads  the run's collect.json, classified.jsonl, classify.json and digest.json
Writes ../openrouter/index.html

Every line the model wrote is shown with numbered links to the posts behind
it. Nothing on the page names a person; posts are linked, not quoted at length.
"""
import argparse
import datetime as dt
import html
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE.parent / "openrouter" / "index.html"
LABEL = {
    "github_openrouter": "OpenRouter's GitHub",
    "github_tools": "Other tools' GitHub",
    "hackernews": "Hacker News",
    "reddit": "Reddit",
}
AREA = {
    "errors_and_failures": "Errors and failed calls", "rate_limits": "Rate limits",
    "credits_and_billing": "Credits and billing", "model_availability": "Which models are there",
    "latency_and_uptime": "Speed and uptime", "provider_routing": "Provider routing",
    "free_models": "Free models", "tool_calling": "Tool calling", "sdks_and_docs": "SDKs and docs",
    "privacy_and_data": "Privacy and data", "pricing": "Prices", "integrations": "Using it inside other tools",
    "other": "Other",
}
KIND = {
    "bug": "Bug reports", "question": "Questions", "feature_request": "Requests", "complaint": "Complaints",
    "praise": "Praise", "comparison": "Comparisons", "showcase": "Things people built", "news": "News",
    "other": "Other",
}


def e(s):
    return html.escape(str(s or ""), quote=True)


def day(iso):
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{d.day} {d.strftime('%b')}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--repo", default="")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--build-usd", type=float, default=None, help="what the whole build cost, discarded runs included")
    a = ap.parse_args()
    run = Path(a.run) if a.run else sorted((HERE / "runs").glob("*/digest.json"))[-1].parent

    meta = json.loads((run / "collect.json").read_text())
    rows = [json.loads(l) for l in (run / "classified.jsonl").open()]
    cls = json.loads((run / "classify.json").read_text())
    dg = json.loads((run / "digest.json").read_text())
    d, st = dg["digest"], dg["stats"]
    byid = {r["id"]: r for r in rows}

    # hand edits: applied on top of the model's draft, and listed on the page
    edited = []
    ef = run / "edits.json"
    if ef.exists():
        ed = json.loads(ef.read_text())
        if "headline" in ed:
            d["headline"], d["headline_ids"] = ed["headline"]["text"], ed["headline"].get("ids", [])
            edited.append(("The headline", ed["headline"]["why"]))
        if "post_back" in ed:
            d["post_back"] = ed["post_back"]["text"]
            edited.append(("The post back to the community", ed["post_back"]["why"]))
        names = {"themes": "A theme", "for_engineering": "The engineering list", "in_other_tools": "The list for other tools",
                 "answer_once": "A question", "liked": "A line on what people liked"}
        for sec in names:
            if sec not in ed:
                continue
            x = ed[sec]
            lst = d.setdefault(sec, [])
            for k, v in x.items():
                if k.isdigit() and int(k) < len(lst):
                    lst[int(k)].update({kk: vv for kk, vv in v.items() if kk != "why"})
                    edited.append((names[sec], v["why"]))
            if "remove" in x:
                d[sec] = [y for i, y in enumerate(lst) if i not in set(x["remove"])]
                edited.append((names[sec], x["why"]))
            if "add" in x:
                d[sec] = d[sec] + x["add"]

    # one running footnote number per post, in order of first citation
    num = {}

    def cite(ids):
        out = []
        for i in ids:
            if i not in byid:
                continue
            num.setdefault(i, len(num) + 1)
            r = byid[i]
            out.append(f'<a class="cite" href="{e(r["url"])}" title="{e(r["where"])}: {e(r["title"] or r["summary"])}">{num[i]}</a>')
        return '<span class="cites">' + "".join(out) + "</span>" if out else ""

    rel = [r for r in rows if r.get("about_openrouter")]
    sent = Counter(r.get("sentiment") for r in rel)
    cost = cls["cost"]["usd"] + dg["cost"]["usd"]
    ag = cls.get("agreement")
    models_used = list(cls["cost"]["by_model"]) + list(dg["cost"]["by_model"])

    def bar(counter, names, top=8):
        tot = max(counter.values()) if counter else 1
        out = []
        for k, v in counter.most_common(top):
            out.append(f'<div class="row"><span class="k">{e(names.get(k, k))}</span>'
                       f'<span class="b"><i style="width:{100 * v / tot:.0f}%"></i></span><span class="v">{v}</span></div>')
        return "".join(out)

    headline = None  # built first below so its posts are numbered first
    _h = f'{e(d.get("headline"))} {cite(d.get("headline_ids", []))}'
    themes = "".join(f'<li><h3>{e(t["title"])}</h3><p>{e(t["what"])} {cite(t["ids"])}</p></li>'
                     for t in d.get("themes", []))
    headline = _h
    eng = "".join(f'<li><p>{e(x["issue"])} <span class="where">{e(x.get("where"))}</span> {cite(x["ids"])}</p></li>'
                  for x in d.get("for_engineering", []))
    other = "".join(f'<li><p>{e(x["issue"])} <span class="where">{e(x.get("where"))}</span> {cite(x["ids"])}</p></li>'
                    for x in d.get("in_other_tools", []))
    edits = "".join(f"<li><p><b>{e(w)}.</b> {e(why)}</p></li>" for w, why in edited)
    once = "".join(f'<li><p>{e(x["question"])} {cite(x["ids"])}</p></li>' for x in d.get("answer_once", []))
    liked = "".join(f'<li><p>{e(x["what"])} {cite(x["ids"])}</p></li>' for x in d.get("liked", []))
    post = "".join(f"<p>{e(p)}</p>" for p in str(d.get("post_back", "")).split("\n") if p.strip())

    sources = "".join(f'<div class="row"><span class="k">{e(LABEL[s])}</span><span class="v">{st["relevant_by_source"].get(s, 0)} of {n}</span></div>'
                      for s, n in sorted(st["by_source"].items(), key=lambda x: -x[1]))
    notes = "".join(
        f'<li id="n{n}"><a href="{e(byid[i]["url"])}">{n}</a> <span class="where">{e(byid[i]["where"])}, {day(byid[i]["created"])}</span> {e(byid[i]["title"] if byid[i]["source"] != "hackernews" and byid[i]["title"] else byid[i]["summary"] or byid[i]["title"])}</li>'
        for i, n in sorted(num.items(), key=lambda x: x[1]))

    agree = ""
    if ag and ag.get("sample"):
        n = ag["sample"]
        agree = (f'<p>To check the labels, {n} of the posts were labelled again by a second model, '
                 f'<code>{e(ag["check_model"])}</code>. The two agreed on the area of {ag["area"]} of {n}, '
                 f'on the kind of post for {ag["kind"]} of {n}, and on the sentiment of {ag["sentiment"]} of {n}. '
                 f'Area agrees least, so read the area chart as rough; the themes cite their posts and can be checked one by one.</p>')
    repo = (f'<a href="{e(a.repo)}">The code</a> is public: one Python file for the reading and the labelling, one for this page, '
            f'standard library only.' if a.repo else
            'The code (one Python file for the reading and the labelling, one for this page, standard library only) is published with this page.')
    ws, we = day(meta["window_start"]), day(meta["window_end"])

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>OpenRouter community pulse, {ws} to {we}</title>
<meta name="robots" content="noindex, nofollow" />
<meta name="description" content="One week of what developers said about OpenRouter in public, read and sorted by a small tool built on OpenRouter's API. Made by Mikyö Clark as part of an application." />
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,200..800&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{ --ground:#0e1214; --raise:#151a1c; --type:#f2efe6; --soft:rgba(242,239,230,.74); --dim:rgba(242,239,230,.52); --rule:rgba(242,239,230,.14); --chalk:#d9c77a; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
html, body {{ background:var(--ground); color:var(--type); }}
body {{ font-family:'Bricolage Grotesque',-apple-system,sans-serif; font-size:18px; line-height:1.55; -webkit-font-smoothing:antialiased; overflow-x:clip; }}
a {{ color:inherit; }}
::selection {{ background:var(--chalk); color:var(--ground); }}
.wrap {{ max-width:760px; margin:0 auto; padding:0 24px; }}
header {{ padding:72px 0 40px; border-bottom:1px solid var(--rule); }}
.kicker, .where, .v, .k, code, .cite, .foot {{ font-family:'Geist Mono',monospace; }}
.kicker {{ font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); }}
h1 {{ font-size:clamp(34px,6vw,52px); line-height:1.05; font-weight:600; letter-spacing:-.02em; margin:14px 0 18px; }}
h2 {{ font-size:13px; font-family:'Geist Mono',monospace; font-weight:500; letter-spacing:.08em; text-transform:uppercase; color:var(--chalk); margin:0 0 18px; }}
h3 {{ font-size:21px; font-weight:600; line-height:1.25; margin-bottom:6px; }}
.lede {{ font-size:21px; color:var(--soft); max-width:640px; }}
.headline {{ font-size:24px; line-height:1.35; font-weight:500; margin-top:28px; padding-left:16px; border-left:2px solid var(--chalk); }}
section {{ padding:44px 0; border-bottom:1px solid var(--rule); }}
p {{ color:var(--soft); }}
p + p {{ margin-top:12px; }}
ul.list {{ list-style:none; }}
ul.list li {{ padding:14px 0; border-top:1px solid var(--rule); }}
ul.list li:first-child {{ border-top:0; padding-top:0; }}
.cites {{ white-space:nowrap; }}
.cite {{ display:inline-block; min-width:20px; padding:0 5px; margin-left:3px; font-size:11px; line-height:18px; text-align:center; text-decoration:none; color:var(--chalk); border:1px solid rgba(217,199,122,.4); border-radius:9px; vertical-align:2px; }}
.cite:hover {{ background:var(--chalk); color:var(--ground); }}
.where {{ font-size:12px; color:var(--dim); }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:36px; }}
.row {{ display:grid; grid-template-columns:minmax(0,1fr) 90px 40px; gap:10px; align-items:center; font-size:13px; padding:5px 0; }}
.stack .row {{ grid-template-columns:minmax(0,1fr) auto; }}
.k {{ color:var(--soft); font-size:12px; }}
.b {{ height:6px; background:var(--raise); border-radius:3px; overflow:hidden; }}
.b i {{ display:block; height:100%; background:var(--chalk); opacity:.8; }}
.v {{ text-align:right; color:var(--type); font-size:12px; }}
.figs {{ display:flex; flex-wrap:wrap; gap:28px; margin:8px 0 28px; }}
.figs div b {{ display:block; font-size:34px; font-weight:600; line-height:1; }}
.figs div span {{ font-family:'Geist Mono',monospace; font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.06em; }}
.post {{ background:var(--raise); border:1px solid var(--rule); border-radius:10px; padding:22px 24px; }}
.post p {{ color:var(--type); }}
code {{ font-size:.85em; color:var(--type); }}
p a, li a:not(.cite) {{ text-decoration:none; border-bottom:1px solid rgba(217,199,122,.45); }}
p a:hover {{ color:var(--chalk); }}
ol.notes {{ list-style:none; font-size:14px; }}
ol.notes li {{ padding:6px 0; color:var(--soft); border-top:1px solid var(--rule); }}
ol.notes li a {{ font-family:'Geist Mono',monospace; font-size:12px; color:var(--chalk); margin-right:6px; }}
details summary {{ cursor:pointer; font-family:'Geist Mono',monospace; font-size:13px; color:var(--chalk); }}
details[open] summary {{ margin-bottom:12px; }}
.foot {{ padding:40px 0 64px; font-size:12px; color:var(--dim); }}
@media (max-width:640px) {{
  body {{ font-size:17px; }}
  header {{ padding:48px 0 32px; }}
  .lede {{ font-size:19px; }} .headline {{ font-size:21px; }}
  .grid {{ grid-template-columns:1fr; gap:28px; }}
  .row {{ grid-template-columns:minmax(0,1fr) 60px 34px; }}
}}
</style>
</head>
<body>
<header><div class="wrap">
  <div class="kicker">Community pulse · {ws} to {we} {dt.date.fromisoformat(meta['window_end'][:10]).year}</div>
  <h1>What developers said about OpenRouter this week</h1>
  <p class="lede">A small tool read {st['items']} public posts that mention OpenRouter, sorted them with models called through OpenRouter's own API, and wrote what a community team would want on Monday. It cost ${cost:.2f} to run.</p>
  <p class="headline">{headline}</p>
</div></header>

<main class="wrap">
<section>
  <h2>The week</h2>
  <div class="figs">
    <div><b>{st['items']}</b><span>posts read</span></div>
    <div><b>{st['about_openrouter']}</b><span>about OpenRouter itself</span></div>
    <div><b>{sent.get(1, 0)} / {sent.get(0, 0)} / {sent.get(-1, 0)}</b><span>warm / neutral / cold</span></div>
  </div>
  <div class="grid">
    <div><p class="where" style="margin-bottom:8px">What they were about</p>{bar(Counter(st['by_area']), AREA)}</div>
    <div><p class="where" style="margin-bottom:8px">What kind of post</p>{bar(Counter(st['by_kind']), KIND)}</div>
  </div>
</section>

<section>
  <h2>Themes</h2>
  <ul class="list">{themes}</ul>
</section>

<section>
  <h2>For engineering</h2>
  <ul class="list">{eng}</ul>
</section>

<section>
  <h2>In tools built on OpenRouter</h2>
  <p style="margin-bottom:14px">Problems OpenRouter's users hit in other tools. They are not OpenRouter's bugs, and they still reach its community and its support queue.</p>
  <ul class="list">{other}</ul>
</section>

<section>
  <h2>Answer once</h2>
  <p style="margin-bottom:14px">Questions that came up more than once, or that many people will have. Each is a pinned answer, a docs fix or a bot reply away from being asked for the last time.</p>
  <ul class="list">{once}</ul>
</section>

<section>
  <h2>What people liked</h2>
  <ul class="list">{liked}</ul>
</section>

<section>
  <h2>A post back to the community</h2>
  <p style="margin-bottom:14px">Drafted from the week for a community lead to rewrite and send. It names what was heard and asks one question back. It promises nothing.</p>
  <div class="post">{post}</div>
</section>

<section>
  <h2>How it works</h2>
  <p>The tool reads four public channels with no sign-in: issues opened in OpenRouter's GitHub repositories; issues that mention OpenRouter in the trackers of {len(meta['tools'])} widely used AI developer tools; Hacker News stories and comments; and Reddit's public search feed. It keeps each post's link, text and date and drops usernames.</p>
  <p>Ten posts at a time go to <code>{e(cls['model'])}</code>, which marks whether the post is about OpenRouter itself, what kind of post it is, what part of the product it touches, how it feels, and the question behind it. The posts about OpenRouter then go to <code>{e(dg['model'])}</code>, which writes the themes and lists above and must cite the posts behind every line. Citations that do not match a real post are dropped before this page is built{f" ({dg['dropped_ids']} this week)" if dg.get('dropped_ids') else ""}.</p>
  {agree}
  <p>The run made {cls['cost']['calls'] + dg['cost']['calls']} model calls across {len(set(models_used))} models and cost ${cost:.2f}, read from OpenRouter's own usage accounting{f"; building the tool, two discarded runs included, cost ${a.build_usd:.2f}" if a.build_usd else ""}. Two things broke along the way: one response came back with no answer in it, and the cheapest models now and then returned JSON that would not parse. The tool now retries the first and re-sends the second one post at a time. {repo}</p>
  {f'<h3 style="margin-top:26px">Edited by hand</h3><p style="margin-bottom:10px">The model drafts; a person decides what goes out. These lines were changed before this page was built, and the edits are kept with the run.</p><ul class="list">{edits}</ul>' if edited else ''}
  <div class="grid" style="margin-top:22px">
    <div class="stack"><p class="where" style="margin-bottom:8px">Posts about OpenRouter, by channel</p>{sources}</div>
    <div class="stack"><p class="where" style="margin-bottom:8px">Models people named most</p>{"".join(f'<div class="row"><span class="k">{e(m)}</span><span class="v">{n}</span></div>' for m, n in list(st['models'].items())[:8])}</div>
  </div>
</section>

<section>
  <h2>What it cannot see</h2>
  <p>The Discord, where most of the conversation happens. Pointed at a Discord export, or run as a bot with read access, the same two steps produce the same page from inside. It also misses X, which has no public search, and anything said in private.</p>
  <p>A week is one reading. The pulse becomes useful when the same page lands every Monday and the counts can be read against the week before.</p>
</section>

<section>
  <h2>Who made this</h2>
  <p>Mikyö Clark. I ran community at EigenLayer through both stakedrops and the token launch, with a Discord of about 200,000, and built its first ambassador program and fellowship and the retroactive distribution of 6M EIGEN to contributors. At Ritual I led marketing while the Discord grew from 18,000 to 109,000. Before crypto I co-led Culture Amp's global community of people leaders. The record, with sources, is at <a href="https://www.mikyo.one/world/">mikyo.one/world</a>.</p>
  <p>I made this page as part of an application to OpenRouter. It is built only from public posts and OpenRouter's public API, and OpenRouter did not ask for it or review it.</p>
</section>

<section>
  <details><summary>Every post cited above ({len(num)})</summary>
  <p class="where" style="margin-bottom:10px">GitHub issues and Reddit posts by their own titles; Hacker News comments by a one-line summary of the comment.</p>
  <ol class="notes">{notes}</ol></details>
</section>
</main>
<footer class="wrap foot">Run {e(run.name)} · window {e(meta['window_start'])} to {e(meta['window_end'])} UTC · <a href="https://www.mikyo.one/">mikyo.one</a></footer>
</body>
</html>
"""
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"wrote {out} ({len(num)} posts cited)")


if __name__ == "__main__":
    main()
