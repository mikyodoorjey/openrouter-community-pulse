#!/usr/bin/env python3
"""Community pulse for OpenRouter: one week of what developers say in public.

Usage:
  python3 pulse.py collect  [--days 7] [--out runs/<date>]
  python3 pulse.py classify [--run runs/<date>] [--model M] [--check-model M2] [--max-usd 3]
  python3 pulse.py digest   [--run runs/<date>] [--model M] [--max-usd 3]
  python3 pulse.py all      [--days 7]

collect   reads public channels with no sign-in: issues in OpenRouter's own
          GitHub repositories, issues that mention OpenRouter in the trackers
          of widely used AI developer tools, Hacker News stories and comments,
          and Reddit's public search feed. Writes items.jsonl.
classify  sends the items, ten at a time, to a small model through
          OpenRouter's API and asks for kind, area, sentiment, models named,
          a one-line summary and the question behind it, if any. With
          --check-model it classifies a sample again on a second model and
          records how often the two agree. Writes classified.jsonl.
digest    groups the classified items and asks a larger model to write the
          week's themes, the bugs worth an engineer's look, the questions to
          answer once, and a post back to the community. Every claim must cite
          item ids; ids that do not exist are dropped. Writes digest.json.

The key is read from OPENROUTER_API_KEY or from ~/.config/openrouter-pulse/key.
It is never printed or written anywhere else. Each run records its own cost
from OpenRouter's usage accounting, and stops before passing --max-usd.
Usernames are not stored: an item keeps its link, its text and its date.
Standard library only.
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
API = "https://openrouter.ai/api/v1/chat/completions"

# Trackers of widely used AI developer tools, where OpenRouter users file
# issues about it. Names as GitHub resolves them on 2026-11-07.
TOOLS = [
    "cline/cline", "RooCodeInc/Roo-Code", "Kilo-Org/kilocode", "open-webui/open-webui",
    "BerriAI/litellm", "Aider-AI/aider", "continuedev/continue", "SillyTavern/SillyTavern",
    "zed-industries/zed", "anomalyco/opencode", "charmbracelet/crush", "aaif-goose/goose",
    "LibreChat-AI/LibreChat", "n8n-io/n8n", "NousResearch/hermes-agent", "openai/codex",
    "vercel/ai", "langchain-ai/langchainjs", "run-llama/llama_index", "crewAIInc/crewAI",
    "janhq/jan", "lobehub/lobehub", "google-gemini/gemini-cli", "anthropics/claude-code",
    "microsoft/autogen",
]

KINDS = ["bug", "question", "feature_request", "complaint", "praise", "comparison", "showcase", "news", "other"]
AREAS = ["errors_and_failures", "rate_limits", "credits_and_billing", "model_availability",
         "latency_and_uptime", "provider_routing", "free_models", "tool_calling",
         "sdks_and_docs", "privacy_and_data", "pricing", "integrations", "other"]


# ---------- plumbing ----------

def get(url, accept="application/json", tries=3):
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                print(f"  fetch failed: {url[:90]} ({e})", file=sys.stderr)
                return None
            time.sleep(4 * (i + 1))


def gh(query, page=1):
    """GitHub issue search through the gh CLI (read-only; uses the signed-in account's rate limit)."""
    out = subprocess.run(["gh", "api", "-X", "GET", "search/issues", "-f", f"q={query}",
                          "-f", "per_page=100", "-f", f"page={page}"],
                         capture_output=True, text=True)
    if out.returncode:
        print(f"  gh search failed: {query} ({out.stderr.strip()[:120]})", file=sys.stderr)
        return {"items": [], "total_count": 0}
    return json.loads(out.stdout)


def clip(s, n):
    s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()
    s = re.sub(r"submitted by\s+/?u/\S+", "", s)            # Reddit bylines
    s = re.sub(r"(?<![\w/])/?u/[A-Za-z0-9_-]{3,}", "u/[user]", s)  # Reddit handles
    s = re.sub(r"(?<![\w.])@[A-Za-z0-9_]{2,39}\b", "@[user]", s)   # GitHub and X handles
    return s[:n].strip()


def key():
    k = os.environ.get("OPENROUTER_API_KEY")
    if not k:
        p = Path.home() / ".config" / "openrouter-pulse" / "key"
        if p.exists():
            k = p.read_text().strip()
    if not k:
        sys.exit("No key: set OPENROUTER_API_KEY or write it to ~/.config/openrouter-pulse/key")
    return k


class Meter:
    """Running cost of model calls, from OpenRouter's usage accounting."""

    def __init__(self, cap):
        self.cap, self.usd, self.calls, self.tin, self.tout = cap, 0.0, 0, 0, 0
        self.by_model = defaultdict(lambda: {"calls": 0, "usd": 0.0, "in": 0, "out": 0, "seconds": 0.0})

    def call(self, model, messages, max_tokens=2000, temperature=0):
        if self.usd >= self.cap:
            sys.exit(f"Stopped: spend reached ${self.usd:.3f}, the cap is ${self.cap}")
        body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "response_format": {"type": "json_object"},
                "usage": {"include": True}}
        req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {key()}", "Content-Type": "application/json",
            "HTTP-Referer": "https://www.mikyo.one/openrouter/", "X-Title": "Community pulse"})
        t0 = time.time()
        d = {}
        for i in range(4):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    d = json.loads(r.read())
            except urllib.error.HTTPError as e:
                msg = e.read().decode()[:300]
                if e.code in (402, 401, 403):
                    sys.exit(f"Model call refused ({e.code}): {msg}")
                d = {"error": {"code": e.code, "message": msg}}
            except Exception as e:  # noqa: BLE001  timeouts, dropped connections
                d = {"error": {"message": str(e)}}
            if d.get("choices"):
                break
            # an error can also arrive inside a 200 response, from the provider behind the model
            print(f"  retry {i + 1}: {str(d.get('error'))[:160]}", file=sys.stderr)
            time.sleep(6 * (i + 1))
        if not d.get("choices"):
            return {}
        u = d.get("usage") or {}
        cost = float(u.get("cost") or 0)
        m = self.by_model[model]
        m["calls"] += 1
        m["usd"] += cost
        m["in"] += u.get("prompt_tokens", 0)
        m["out"] += u.get("completion_tokens", 0)
        m["seconds"] += time.time() - t0
        self.usd += cost
        self.calls += 1
        text = d["choices"][0]["message"]["content"] or ""
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        for cand in (text, (re.search(r"\{.*\}", text, re.S) or [None])[0]):
            if not cand:
                continue
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                pass
        print(f"  unreadable JSON from {model}; the items will be retried one by one", file=sys.stderr)
        return {}

    def report(self):
        return {"usd": round(self.usd, 4), "calls": self.calls,
                "by_model": {k: {**v, "usd": round(v["usd"], 4), "seconds": round(v["seconds"], 1)}
                             for k, v in self.by_model.items()}}


# ---------- collect ----------

def collect(days, out, only=None):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    sday = since.strftime("%Y-%m-%d")
    items, seen = [], set()
    if only:  # refresh one channel inside an existing run, keeping its window
        meta0 = json.loads((out / "collect.json").read_text())
        now = dt.datetime.fromisoformat(meta0["window_end"])
        since = dt.datetime.fromisoformat(meta0["window_start"])
        for it in map(json.loads, (out / "items.jsonl").open()):
            if it["source"] != only:
                items.append(it)
                seen.add(it["id"])

    def add(it):
        if it["id"] in seen:
            return
        seen.add(it["id"])
        items.append(it)

    if not only or only == "github":
        collect_github(sday, add)
    if not only or only == "hackernews":
        collect_hn(since, add)
    if not only or only == "reddit":
        collect_reddit(since, add)
    write_run(out, items, since, now)


def collect_github(sday, add):
    print("GitHub: OpenRouter's own repositories")
    page = 1
    while True:
        d = gh(f"org:OpenRouterTeam is:issue created:>={sday}", page)
        for x in d["items"]:
            if (x.get("user") or {}).get("type") == "Bot":
                continue
            repo = x["repository_url"].split("/")[-1]
            add({"id": f"gh:{x['id']}", "source": "github_openrouter", "where": f"OpenRouterTeam/{repo}",
                 "url": x["html_url"], "title": clip(x["title"], 200), "text": clip(x.get("body"), 1500),
                 "created": x["created_at"], "replies": x.get("comments", 0)})
        if len(d["items"]) < 100 or page >= 5:
            break
        page += 1
        time.sleep(2.2)

    print(f"GitHub: {len(TOOLS)} developer tools' trackers")
    for repo in TOOLS:
        time.sleep(2.2)
        d = gh(f"repo:{repo} openrouter is:issue created:>={sday}")
        for x in d["items"]:
            if (x.get("user") or {}).get("type") == "Bot":
                continue
            add({"id": f"gh:{x['id']}", "source": "github_tools", "where": repo,
                 "url": x["html_url"], "title": clip(x["title"], 200), "text": clip(x.get("body"), 1500),
                 "created": x["created_at"], "replies": x.get("comments", 0)})



def collect_hn(since, add):
    print("Hacker News")
    ts = int(since.timestamp())
    page = 0
    while page < 10:
        q = urllib.parse.urlencode({"query": "openrouter", "tags": "(story,comment)",
                                    "numericFilters": f"created_at_i>{ts}", "hitsPerPage": 200, "page": page})
        raw = get(f"https://hn.algolia.com/api/v1/search_by_date?{q}")
        if not raw:
            break
        d = json.loads(raw)
        for h in d["hits"]:
            is_story = "story" in (h.get("_tags") or [])
            text = h.get("story_text") or h.get("comment_text") or ""
            add({"id": f"hn:{h['objectID']}", "source": "hackernews",
                 "where": "Hacker News story" if is_story else "Hacker News comment",
                 "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                 "title": clip(h.get("title") or h.get("story_title") or "", 200), "text": clip(text, 1500),
                 "created": h["created_at"], "replies": h.get("num_comments") or 0})
        page += 1
        if page >= d.get("nbPages", 0):
            break



def collect_reddit(since, add):
    """Reddit allows an unsigned reader about one request a minute, so this is slow on purpose."""
    print("Reddit (public search feed, one request a minute)")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    feeds = [("https://www.reddit.com/search.rss?q=openrouter&sort=new&t=week&limit=100", 6),
             ("https://www.reddit.com/r/openrouter/new/.rss?limit=100", 3)]
    for base, pages in feeds:
        after = None
        for _ in range(pages):  # 100 entries a page; page back until the window ends
            time.sleep(65)
            raw = get(base + (f"&after={after}" if after else ""), accept="application/atom+xml")
            if not raw or not raw.lstrip().startswith("<"):
                break
            entries = ET.fromstring(raw).findall("a:entry", ns)
            if not entries:
                break
            for e in entries:
                when = (e.findtext("a:updated", default="", namespaces=ns)
                        or e.findtext("a:published", default="", namespaces=ns))
                try:
                    if dt.datetime.fromisoformat(when.replace("Z", "+00:00")) < since:
                        continue
                except ValueError:
                    pass
                cat = e.find("a:category", ns)
                if cat is not None and re.match(r"u[_/]", cat.get("term", "")):
                    continue  # posts on personal profiles: their links carry the username, so they stay out
                add({"id": f"rd:{e.findtext('a:id', namespaces=ns)}", "source": "reddit",
                     "where": f"r/{cat.get('term')}" if cat is not None else "Reddit",
                     "url": e.find("a:link", ns).get("href"),
                     "title": clip(e.findtext("a:title", default="", namespaces=ns), 200),
                     "text": clip(e.findtext("a:content", default="", namespaces=ns), 1500),
                     "created": when, "replies": None})
            after = entries[-1].findtext("a:id", namespaces=ns)
            oldest = entries[-1].findtext("a:updated", default="", namespaces=ns)
            try:
                if dt.datetime.fromisoformat(oldest.replace("Z", "+00:00")) < since:
                    break
            except ValueError:
                break



def write_run(out, items, since, now):
    out.mkdir(parents=True, exist_ok=True)
    items.sort(key=lambda x: x["created"])
    with (out / "items.jsonl").open("w") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    meta = {"window_start": since.isoformat(timespec="minutes"), "window_end": now.isoformat(timespec="minutes"),
            "counts": dict(Counter(i["source"] for i in items)), "tools": TOOLS}
    (out / "collect.json").write_text(json.dumps(meta, indent=1))
    print(f"{len(items)} items -> {out/'items.jsonl'}  {meta['counts']}")


# ---------- classify ----------

CLASSIFY_SYS = f"""You read public developer posts that mention OpenRouter (a single API for hundreds of AI models).
For each item return an object with:
  id: the item id, unchanged
  about_openrouter: true only if a real part of the post is about OpenRouter's service: an experience with it, a problem or error with it, a question about it, its prices or credits, which models it carries, how it routes to providers, its docs or SDKs, a comparison in which OpenRouter is one side, or news about it.
    False when OpenRouter is named in passing: as where someone happens to get a model ("I use X via OpenRouter"), in a list of supported providers, as a config value or environment variable, in a project description, or when the post is about something else.
    Examples. True: "OpenRouter returned 429 on every free model today". True: "Is OpenRouter's BYOK fee worth it versus going direct?". False: "I run GLM and Kimi through OpenRouter for my side project, here is my agent setup". False: "Added support for Ollama, OpenRouter and LM Studio".
  kind: one of {KINDS}
  area: one of {AREAS}
  sentiment: -1, 0 or 1 toward OpenRouter
  models: model names the post names (empty list if none)
  summary: one plain sentence, under 25 words, saying what the person reports or asks. No names of people.
  question: if the post asks or implies a question other developers will also have, that question in under 15 words, phrased generally; else null
Return JSON: {{"items": [ ... ]}} with one object per input item, in order."""


def classify(run, model, check_model, max_usd, batch=10):
    items = [json.loads(l) for l in (run / "items.jsonl").open()]
    meter = Meter(max_usd)

    def label(model_id, chunk):
        payload = [{"id": i["id"], "where": i["where"], "title": i["title"], "text": i["text"][:1200]} for i in chunk]
        r = meter.call(model_id, [{"role": "system", "content": CLASSIFY_SYS},
                                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                       max_tokens=200 * len(chunk) + 200)
        got = {x.get("id"): x for x in (r.get("items") or []) if isinstance(x, dict)}
        return got

    cache = run / "labels.partial.json"  # lets a stopped run resume without paying twice
    out = json.loads(cache.read_text()) if cache.exists() else {}
    costfile = cache.with_suffix(".cost")
    spent0 = json.loads(costfile.read_text()) if costfile.exists() else {"usd": 0, "calls": 0, "by_model": {}}
    todo = [i for i in items if i["id"] not in out]
    for n in range(0, len(todo), batch):
        chunk = todo[n:n + batch]
        got = label(model, chunk)
        missing = [i for i in chunk if i["id"] not in got]
        for i in missing:  # one retry, one by one
            got.update(label(model, [i]))
        out.update(got)
        cache.write_text(json.dumps(out))
        now = meter.report()
        merged = {"usd": spent0["usd"] + now["usd"], "calls": spent0["calls"] + now["calls"], "by_model": dict(spent0["by_model"])}
        for m, v in now["by_model"].items():
            cur = merged["by_model"].setdefault(m, {"calls": 0, "usd": 0, "in": 0, "out": 0, "seconds": 0})
            merged["by_model"][m] = {k2: round(cur.get(k2, 0) + v.get(k2, 0), 4) for k2 in ("calls", "usd", "in", "out", "seconds")}
        costfile.write_text(json.dumps(merged))
        print(f"  {len(out)}/{len(items)}  ${merged['usd']:.3f}", flush=True)

    agree = None
    if check_model:
        sample = [i for i in items if out.get(i["id"], {}).get("about_openrouter")][:60]
        other = {}
        for n in range(0, len(sample), batch):
            other.update(label(check_model, sample[n:n + batch]))
        both = [i["id"] for i in sample if i["id"] in other]
        agree = {
            "check_model": check_model, "sample": len(both),
            "area": sum(out[i].get("area") == other[i].get("area") for i in both),
            "kind": sum(out[i].get("kind") == other[i].get("kind") for i in both),
            "sentiment": sum(out[i].get("sentiment") == other[i].get("sentiment") for i in both),
        }

    with (run / "classified.jsonl").open("w") as fh:
        for i in items:
            c = out.get(i["id"], {})
            fh.write(json.dumps({**i, **{k: c.get(k) for k in
                                         ("about_openrouter", "kind", "area", "sentiment", "models", "summary", "question")}},
                                ensure_ascii=False) + "\n")
    report = meter.report()
    if spent0["calls"]:
        spent = spent0
        if spent:  # add what earlier, stopped attempts already spent
            report["usd"] = round(report["usd"] + spent["usd"], 4)
            report["calls"] += spent["calls"]
            for m, v in spent["by_model"].items():
                cur = report["by_model"].setdefault(m, {"calls": 0, "usd": 0, "in": 0, "out": 0, "seconds": 0})
                for k2 in cur:
                    cur[k2] = round(cur[k2] + v.get(k2, 0), 4)
    (run / "classify.json").write_text(json.dumps({"model": model, "agreement": agree, "cost": report}, indent=1))
    for f in (cache, cache.with_suffix(".cost")):
        f.unlink(missing_ok=True)
    print(f"classified {len(out)}/{len(items)}; spent ${meter.usd:.3f}; agreement {agree}")


# ---------- digest ----------

DIGEST_SYS = """You write a weekly community pulse for the team that runs OpenRouter's developer community.
You get a list of classified public posts (id, where, kind, area, sentiment, summary, question).
Write only what the posts support, and cite the ids of the posts behind every line.
Rules:
- Report what people say as reports, not as facts: "a Reddit thread reports", "one user says", "several posts ask". Give counts only when you counted posts. Never repeat a number from a single post as if it were established.
- Do not name third-party companies or sites as the cause of anything. Do not name people.
- Plain sentences. No adjectives of praise or alarm. No em dashes or en dashes.
- for_engineering is only for problems in OpenRouter's own service, API, billing or code (its SDKs and docs). A bug in another tool that merely uses OpenRouter goes in in_other_tools instead.
Return JSON:
{
 "headline": one sentence: the single most useful thing the team should know this week, worded as a report,
 "themes": [ {"title": short, "what": two sentences, "ids": [...]} ]  (3 to 6, largest first),
 "for_engineering": [ {"issue": one sentence, "where": where it was reported, "ids": [...]} ]  (up to 6),
 "in_other_tools": [ {"issue": one sentence, "where": the tool, "ids": [...]} ]  (problems in tools built on OpenRouter that its users hit; up to 4),
 "answer_once": [ {"question": the question, "ids": [...] } ]  (questions asked more than once, or that many will ask; up to 6),
 "liked": [ {"what": one sentence, "ids": [...]} ]  (up to 4),
 "post_back": a short post (under 120 words) a community lead could put in Discord this week: what was heard, and one question back to the community. It must not say the team is looking into, fixing or planning anything, and must not praise the community's work with adjectives.
}"""


def digest(run, model, max_usd):
    rows = [json.loads(l) for l in (run / "classified.jsonl").open()]
    rel = [r for r in rows if r.get("about_openrouter")]
    meter = Meter(max_usd)
    payload = [{k: r.get(k) for k in ("id", "where", "kind", "area", "sentiment", "summary", "question")} for r in rel]
    d = meter.call(model, [{"role": "system", "content": DIGEST_SYS},
                           {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], max_tokens=4000)
    ids = {r["id"] for r in rel}
    dropped = 0
    for sec in ("themes", "for_engineering", "in_other_tools", "answer_once", "liked"):
        keep = []
        for x in d.get(sec) or []:
            good = [i for i in x.get("ids") or [] if i in ids]
            dropped += len(x.get("ids") or []) - len(good)
            if good:
                keep.append({**x, "ids": good})
        d[sec] = keep
    def tidy(t):
        return re.sub(r"\s*[\u2014\u2013]\s*", ", ", t) if isinstance(t, str) else t
    for k in ("headline", "post_back"):
        d[k] = tidy(d.get(k))
    for sec in ("themes", "for_engineering", "in_other_tools", "answer_once", "liked"):
        d[sec] = [{k: tidy(v) for k, v in x.items()} for x in d[sec]]
    flags = [p for p in (r"looking into", r"we're working", r"we are working", r"we will", r"we'll fix", r"impressive", r"amazing")
             if re.search(p, d.get("post_back") or "", re.I)]
    if flags:
        print(f"  check the post back: it contains {flags}", file=sys.stderr)
    stats = {
        "items": len(rows), "about_openrouter": len(rel),
        "by_source": dict(Counter(r["source"] for r in rows)),
        "relevant_by_source": dict(Counter(r["source"] for r in rel)),
        "by_kind": dict(Counter(r.get("kind") for r in rel).most_common()),
        "by_area": dict(Counter(r.get("area") for r in rel).most_common()),
        "sentiment": dict(Counter(str(r.get("sentiment")) for r in rel)),
        "models": dict(Counter(m for r in rel for m in (r.get("models") or [])).most_common(15)),
    }
    out = {"digest": d, "stats": stats, "model": model, "dropped_ids": dropped, "flags": flags, "cost": meter.report()}
    (run / "digest.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"digest written; {len(rel)} relevant of {len(rows)}; {dropped} bad ids dropped; spent ${meter.usd:.3f}")


def latest_run():
    runs = sorted((HERE / "runs").glob("*/items.jsonl"))
    if not runs:
        sys.exit("no run yet: python3 pulse.py collect")
    return runs[-1].parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["collect", "classify", "digest", "all"])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--only", choices=["github", "hackernews", "reddit"], help="refresh one channel in an existing run")
    ap.add_argument("--run")
    ap.add_argument("--model", default=None)
    ap.add_argument("--check-model", default="anthropic/claude-haiku-4.5")
    ap.add_argument("--max-usd", type=float, default=3.0)
    a = ap.parse_args()
    if a.step in ("collect", "all"):
        out = Path(a.run) if a.run else (latest_run() if a.only else HERE / "runs" / dt.date.today().isoformat())
        collect(a.days, out, a.only)
    run = Path(a.run) if a.run else latest_run()
    if a.step in ("classify", "all"):
        classify(run, a.model or "google/gemini-2.5-flash", a.check_model, a.max_usd)
    if a.step in ("digest", "all"):
        digest(run, a.model or "anthropic/claude-sonnet-4.5", a.max_usd)


if __name__ == "__main__":
    main()
