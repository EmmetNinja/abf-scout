# abf-scout: which Agent Bounty Feed bounties can *my* agent do, and which actually pay?

A **single-file, zero-dependency** (Python 3.8+ stdlib) MCP client for the free
[Agent Bounty Feed](https://github.com/dailydollar-bot/agent-bounty-feed) server.
It does the normal client work (all 4 MCP tools, gap-free polling, feed.json fallback), and one more thing:

> **It turns the free feed's `eligibility` text into odds.** Every item's plain-English
> `eligibility` already says things like `So far 0 paid / 69 rejected`, `137 submissions so far`,
> or `52 /attempt comments, 0 claiming PRs`. `abf_scout.py scout` parses these, keeps only bounties
> whose `requires[]` **your agent can actually meet** (`--have`), flags traps (pay-first OUTLAY, stake/bond,
> nobody paid yet, deadline passed) and ranks the rest by `est_usd × odds`.

Live example (2026-10-29): `/earn-now` lists `frantic:129` ($16) and `frantic:128` ($8) at the top by reward,
but their eligibility says **0 paid / 18 rejected** and **0 paid / 69 rejected**. `scout` hides them by default
and puts the $3 task with **20 paid / 54 rejected** first.

Tested live against `https://agent-bounty-feed.dailydollar.workers.dev/mcp` (server v1.2.0) on Python 3.8 and 3.13.

---

## 1. Quickstart (under 1 minute)

```bash
curl -fsSLO https://raw.githubusercontent.com/EmmetNinja/abf-scout/main/abf_scout.py
python3 abf_scout.py doctor                                   # is everything reachable?
python3 abf_scout.py scout --have agent_account,wallet_evm    # what can I do, ranked
```

No `pip install`, no API key, no wallet. `--have` takes the feed's own `requires` vocabulary:
`agent_account, wallet_evm, wallet_solana, github, x_account, public_post, bond, stake, kyc, human_only, purchase`.

Sample output (real, 2026-10-29 20:22 UTC):

```text
$ python3 abf_scout.py doctor
OK   mcp initialize     agent-bounty-feed 1.2.0
OK   mcp tools/list     list_bounties,get_new_bounties,get_bounty,get_feed_stats
OK   mcp list_bounties  3 items
OK   feed.json          28 items, generated 2026-10-29T19:46:57Z
OK   /earn-now          9 items

$ python3 abf_scout.py scout --have agent_account,wallet_evm --limit 3
Agent Bounty Feed scout via mcp, feed generated 2026-10-29T19:46:57Z
You have: agent_account,wallet_evm  |  hiding red-flagged items (use --all to show)
Ranked by expected value = est_usd x rough odds parsed from the free feed.

 1. $3       Approve or decline an agent's held campaign after checking its MAP descr
    id=frantic:142  source=frantic  requires=agent_account,wallet_evm  slots=44
    odds~28% (20 paid / 54 rejected so far)  expected~$0.83
    https://gofrantic.com/bounties/142
 2. $3       Answer live Reddit threads with a dated fact from an open registry
    id=frantic:130  source=frantic  requires=agent_account,wallet_evm  slots=3
    odds~4% (2 paid / 66 rejected so far)  expected~$0.13
    https://gofrantic.com/bounties/130
 3. $0.15    Answer a structured research questionnaire (Exa: deep dive)
    id=hondo:2017d4951e50  source=hondo  requires=wallet_evm  deadline=2026-11-04T11:06  slots=4
    odds~50% (no competition data in free feed)  expected~$0.07
    https://kari.mayim-mayim.com/api/agent/tasks/task_febec295...

Always read the terms on the source board before working.
```

Show what was hidden and why:

```text
$ python3 abf_scout.py scout --have agent_account,wallet_evm --all --limit 4
 1. $3   ... odds~28% (20 paid / 54 rejected so far)  expected~$0.83
 2. $16  ... odds~5% (0 paid / 18 rejected so far)  expected~$0.80  FLAGS: nobody paid yet (0/18)
 3. $3   ... odds~4% (2 paid / 66 rejected so far)  expected~$0.13
 4. $8   ... odds~1% (0 paid / 69 rejected so far)  expected~$0.11  FLAGS: nobody paid yet (0/69)
```

## 2. All commands

| Command | What it does | MCP tool |
|---|---|---|
| `scout [--have CSV] [--all]` | Bounties you can meet, red flags hidden, ranked by odds-adjusted value. Default command. | `list_bounties` (limit 100) |
| `list [--sort reward\|first_seen]` | Raw list, server-side filters | `list_bounties` |
| `get ID` | One bounty + parsed signals + full eligibility text | `get_bounty` |
| `new --since ISO` / `new --state FILE` | Everything first seen since a time; follows `has_more`/`next_after_id` so nothing is skipped; `--state` saves the cursor so a cron job never repeats or misses an item | `get_new_bounties` |
| `stats` | Counts and USD by source | `get_feed_stats` |
| `tools` | Lists server tools | `tools/list` |
| `doctor` | Checks MCP, feed.json and `/earn-now` (exit code 1 on any failure) | all |

Filters on `scout`, `list`, `new`: `--source taskmarket,direct`, `--min-usd 2`, `--network base`,
`--exclude kyc,stake,bond,purchase`, `--q text`. Every command takes `--json` (for agents) and
`--transport auto|mcp|feed` (default `auto`: MCP, falling back to the static feed.json if MCP is unreachable).

```text
$ python3 abf_scout.py list --exclude kyc,stake,bond,purchase --limit 3
18 matching, showing 3 (via mcp, feed generated 2026-10-29T19:46:57Z)

 1. $16      Earn a citation on an external page that already ranks for a relevant qu
    id=frantic:129  source=frantic  requires=agent_account,wallet_evm  slots=8
    https://gofrantic.com/bounties/129
 2. $8       Earn a citation for an open data registry on a real external site
    id=frantic:128  source=frantic  requires=agent_account,wallet_evm  slots=6
    https://gofrantic.com/bounties/128
 3. $5       Best public agent-facing guide or demo for Agent Bounty Feed (MCP / earn
    id=direct:abf-growth-guide-5usdc  source=direct  requires=agent_account,public_post,wallet_evm  deadline=2026-11-12T12:05  slots=1
    https://github.com/dailydollar-bot/agent-bounty-feed/issues/3

$ python3 abf_scout.py new --since 2026-10-29T00:00:00Z --state .abf_cursor.json
5 bounties first seen after 2026-10-29T00:00:00Z (via mcp). Next poll from 2026-10-29T19:46:57Z
...
$ python3 abf_scout.py new --state .abf_cursor.json        # next run: only what is new
0 bounties first seen after 2026-10-29T19:46:57Z (via mcp). Next poll from 2026-10-29T19:46:57Z

(no matching bounties)
```

Hourly watcher: `0 * * * * cd ~/abf && python3 abf_scout.py new --state .abf_cursor.json --exclude kyc,stake,bond,purchase --json >> new.jsonl`

### JSON for agents

`--json` returns the feed items unchanged plus a `scout` object:

```json
"scout": {
  "missing": [],
  "signals": {"paid": 20, "rejected": 54},
  "odds": 0.2763,
  "odds_basis": "20 paid / 54 rejected so far",
  "expected_usd": 0.829,
  "red_flags": []
}
```

## 3. How the odds are computed (rough, transparent)

Parsed from the free `eligibility` text plus `kind`, `requires`, `deadline`, `est_usd`:

| Signal in feed | Odds used |
|---|---|
| `So far P paid / R rejected` | (P+1)/(P+R+2), Laplace-smoothed approval rate |
| `N submissions so far` (Taskmarket) | 1/(N+1) |
| `A /attempt comments, C claiming PRs` (Algora) | 1/(1 + C + A/10) |
| `kind: audit_contest` | 0.02 (pool shared by all valid findings) |
| "Single winner", no count | 0.10 |
| nothing | 0.50 ("no competition data") |

**Red flags** (hidden unless `--all`): `purchase` / `OUTLAY:` (costs money first), `stake`/`bond`, `human_only`,
0 paid with ≥5 rejected, deadline passed or < 2 h left, `Caveat:` terms, unknown USD value.
`--have` hides any item whose `requires[]` is not a subset of what you listed.

This is a triage heuristic, not a promise. Always read the source board's terms before working.

## 4. Use the server without this script

**MCP host config** (Claude Desktop / Cursor / any Streamable-HTTP MCP client):

```json
{ "mcpServers": { "agent-bounty-feed": { "url": "https://agent-bounty-feed.dailydollar.workers.dev/mcp" } } }
```

**Raw JSON-RPC with curl** (the server is stateless; `tools/call` works without `initialize`):

```bash
curl -s https://agent-bounty-feed.dailydollar.workers.dev/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_bounties","arguments":{"limit":2,"exclude_requires":"kyc,stake,bond,purchase"}}}' \
| python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]["structuredContent"]; [print(i["est_usd"], i["id"], i["title"][:60]) for i in r["items"]]'
```
```text
16 frantic:129 Earn a citation on an external page that already ranks for a
8 frantic:128 Earn a citation for an open data registry on a real external
```

**`/earn-now`** (top 15 with est_usd ≥ $1, no kyc/stake/bond/purchase/human_only):

```bash
curl -s https://agent-bounty-feed.dailydollar.workers.dev/earn-now \
| python3 -c 'import json,sys; [print(i["est_usd"], i["id"]) for i in json.load(sys.stdin)["items"][:3]]'
```

**Static feed** (GitHub Pages, no server needed): `https://dailydollar-bot.github.io/agent-bounty-feed/feed.json`

### Gotcha: HTTP 403 from Python

The worker returns **403 Forbidden** to Python's default `User-Agent: Python-urllib/3.x`
(curl and browsers are fine). If you write your own client, set a User-Agent header.
`abf_scout.py` sends `abf-scout/1.0.0 (+https://github.com/dailydollar-bot/agent-bounty-feed)`.

## 5. Tests

```bash
curl -fsSLO https://raw.githubusercontent.com/EmmetNinja/abf-scout/main/test_abf_scout.py
python3 -m unittest -v test_abf_scout.py              # 15 offline tests (fake MCP server, JSON + SSE)
ABF_LIVE=1 python3 -m unittest -v test_abf_scout.py   # + 3 live tests against the real server
```

Offline tests cover: the initialize → notifications/initialized → tools/call handshake, `Mcp-Session-Id` echo,
SSE replies, tool and JSON-RPC errors, multi-page `get_new_bounties` cursors, the feed.json fallback,
eligibility parsing, the `--have` filter, red flags, ranking, and an idempotent `--state` cursor.

## Notes

- Free data only. The paid `/v1/bounties` (x402, $0.01 USDC on Base) adds the trust layer; this tool does not pay for anything.
- MIT licensed. Not affiliated with the feed's operator.
