# OpenRouter community pulse

A small tool that reads a week of what developers say about OpenRouter in public, sorts it with models called through OpenRouter's own API, and writes what a community team would want on Monday: the themes, the bugs worth an engineer's look, the questions to answer once, and a post back to the community. Every line it writes cites the posts behind it.

The page it produced for 31 Oct to 7 Nov 2026 is at [mikyo.one/openrouter](https://www.mikyo.one/openrouter/). I built it as part of an application to OpenRouter; OpenRouter did not ask for it or review it.

## Run it

```
python3 pulse.py collect             # GitHub, Hacker News and Reddit; public, no sign-in, no key
python3 pulse.py classify            # a small model labels each post
python3 pulse.py digest              # a larger model writes the week, citing posts
python3 render.py --out index.html   # the page
```

`collect --only reddit` refreshes Reddit alone: Reddit allows an unsigned reader about one request a minute.

It needs Python 3 and nothing else, plus the `gh` CLI for GitHub search. The OpenRouter key is read from `OPENROUTER_API_KEY` or `~/.config/openrouter-pulse/key`; it is never printed or written. Each run records its cost from OpenRouter's usage accounting and stops at `--max-usd` (default $3). The week above cost $0.34.

## What it keeps

Each post's link, text and date. Usernames are stripped, and Reddit posts on personal profiles are skipped because their links carry the username. The posts themselves are not in this repository; `runs/2026-11-06/` holds the run's counts, the digest, its cost, and `edits.json`: the lines I changed by hand after the model wrote them, each with the reason. The page lists those edits too.

## What it cannot see

The Discord, where most of the conversation happens; X, which has no public search; anything said in private. Pointed at a Discord export, the same two model steps would produce the same page from inside.
