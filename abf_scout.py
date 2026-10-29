#!/usr/bin/env python3
"""abf_scout: which Agent Bounty Feed bounties can MY agent do, and which are worth it?

Single file, Python 3.8+ standard library only. Talks to the free Agent Bounty Feed
MCP server (Streamable HTTP, JSON-RPC 2.0) and falls back to the static feed.json.

The free feed hides the paid trust layer, but each item's plain-English `eligibility`
text still carries competition and payout signals ("137 submissions so far",
"So far 20 paid / 54 rejected", "42 /attempt comments, 1 claiming PRs", "OUTLAY: ...").
abf_scout parses those into fields, keeps only bounties whose `requires[]` your agent
can actually meet (--have), and ranks them by a rough, transparent odds-adjusted value.

  python3 abf_scout.py scout --have agent_account,wallet_evm
  python3 abf_scout.py list --exclude kyc,stake,bond,purchase --limit 5
  python3 abf_scout.py get direct:abf-growth-guide-5usdc
  python3 abf_scout.py new --state .abf_cursor.json      # gap-free, cron-safe
  python3 abf_scout.py stats | doctor | tools

Add --json to any command for machine-readable output.
"""
import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

__version__ = "1.0.0"

MCP_URL = "https://agent-bounty-feed.dailydollar.workers.dev/mcp"
FEED_URL = "https://dailydollar-bot.github.io/agent-bounty-feed/feed.json"
EARN_NOW_URL = "https://agent-bounty-feed.dailydollar.workers.dev/earn-now"
# The worker answers 403 to Python's default "Python-urllib/x.y" User-Agent,
# so every request sends an explicit one.
USER_AGENT = "abf-scout/%s (+https://github.com/dailydollar-bot/agent-bounty-feed)" % __version__
PROTOCOL_VERSION = "2025-03-26"
REQUIRES_VOCAB = ("agent_account", "wallet_evm", "wallet_solana", "bond", "stake", "kyc",
                  "github", "x_account", "public_post", "human_only", "purchase")


class ABFError(Exception):
    pass


# --------------------------------------------------------------------------- transport
def http_post_json(url, payload, headers=None, timeout=30):
    body = json.dumps(payload).encode()
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        return r.headers.get("Content-Type", ""), r.headers.get("Mcp-Session-Id"), r.read().decode("utf-8")


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_rpc_body(content_type, text, want_id):
    """Return the JSON-RPC message with id == want_id from a JSON or SSE body."""
    if "text/event-stream" in (content_type or ""):
        msgs, data = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif line == "" and data:
                msgs.append(json.loads("\n".join(data)))
                data = []
    else:
        parsed = json.loads(text) if text.strip() else []
        msgs = parsed if isinstance(parsed, list) else [parsed]
    for m in msgs:
        if isinstance(m, dict) and m.get("id") == want_id:
            return m
    raise ABFError("no JSON-RPC response with id %r in server reply" % want_id)


class MCPClient:
    """Minimal Streamable-HTTP MCP client: initialize once, then tools/list and tools/call."""

    def __init__(self, url=MCP_URL, post=http_post_json, timeout=30):
        self.url, self.post, self.timeout = url, post, timeout
        self.session_id, self._id, self.server_info = None, 0, None

    def _rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        ctype, sid, text = self.post(self.url, msg, headers, self.timeout)
        if sid:
            self.session_id = sid
        if notify:
            return None
        resp = parse_rpc_body(ctype, text, msg["id"])
        if "error" in resp:
            raise ABFError("MCP error %s: %s" % (resp["error"].get("code"), resp["error"].get("message")))
        return resp["result"]

    def initialize(self):
        if self.server_info is None:
            res = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "abf-scout", "version": __version__}})
            self.server_info = res.get("serverInfo", {})
            self._rpc("notifications/initialized", notify=True)
        return self.server_info

    def list_tools(self):
        self.initialize()
        return self._rpc("tools/list").get("tools", [])

    def call(self, name, arguments=None):
        self.initialize()
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise ABFError(text or "tool %s failed" % name)
        if "structuredContent" in res:
            return res["structuredContent"]
        return json.loads(text)


# --------------------------------------------------------------------------- feed.json fallback
def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def filter_items_locally(items, source=None, min_usd=None, network=None, exclude_requires=None, q=None):
    """Same semantics as the server-side list_bounties filters, for the feed.json fallback."""
    out = []
    srcs, excl = set(_csv(source)), set(_csv(exclude_requires))
    for it in items:
        if srcs and it.get("source") not in srcs:
            continue
        if min_usd and (it.get("est_usd") is None or it["est_usd"] < min_usd):
            continue
        if network and not str(it.get("network") or "").lower().startswith(network.lower()):
            continue
        if excl & set(it.get("requires") or []):
            continue
        if q and q.lower() not in ("%s %s" % (it.get("title", ""), it.get("eligibility", ""))).lower():
            continue
        out.append(it)
    return out


class Feed:
    """list/get/new/stats through MCP; if MCP is unreachable, through feed.json."""

    def __init__(self, transport="auto", mcp=None, get_json=http_get_json):
        self.transport, self.mcp, self.get_json = transport, mcp or MCPClient(), get_json
        self.used = None
        self._feed = None

    def _feed_json(self):
        if self._feed is None:
            self._feed = self.get_json(FEED_URL)
        return self._feed

    def _via(self, mcp_fn, feed_fn):
        if self.transport in ("auto", "mcp"):
            try:
                r = mcp_fn()
                self.used = "mcp"
                return r
            except (urllib.error.URLError, OSError, ValueError, ABFError) as e:
                if self.transport == "mcp" or (isinstance(e, ABFError) and "No open bounty" in str(e)):
                    raise
                sys.stderr.write("abf_scout: MCP unavailable (%s); falling back to feed.json\n" % e)
        self.used = "feed.json"
        return feed_fn()

    def list(self, limit=100, sort="reward", **filters):
        args = {k: v for k, v in filters.items() if v not in (None, "", [])}
        args.update(limit=min(max(int(limit), 1), 100), sort=sort)

        def from_feed():
            items = filter_items_locally(self._feed_json()["items"], **filters)
            if sort == "first_seen":
                items.sort(key=lambda i: i.get("first_seen") or "", reverse=True)
            return {"generated_at": self._feed_json()["generated_at"], "items": items[:args["limit"]],
                    "total_matching": len(items)}
        return self._via(lambda: self.mcp.call("list_bounties", args), from_feed)

    def get(self, bounty_id):
        def from_feed():
            for it in self._feed_json()["items"]:
                if it["id"] == bounty_id:
                    return it
            raise ABFError("No open bounty with id %s (it may have closed)." % bounty_id)
        def from_mcp():
            r = self.mcp.call("get_bounty", {"id": bounty_id})
            return r.get("item", r)
        return self._via(from_mcp, from_feed)

    def stats(self):
        def from_feed():
            f = self._feed_json()
            return {"generated_at": f["generated_at"], "stats": f["stats"]}
        return self._via(lambda: self.mcp.call("get_feed_stats"), from_feed)

    def new_since(self, since, after_id=None, max_pages=50, **filters):
        """Follow get_new_bounties to the end (gap-free). Returns (items, next_since)."""
        def via_mcp():
            items, s, a = [], since, after_id
            for _ in range(max_pages):
                args = {k: v for k, v in filters.items() if v not in (None, "", [])}
                args.update(since=s, limit=100)
                if a:
                    args["after_id"] = a
                page = self.mcp.call("get_new_bounties", args)
                items.extend(page.get("items", []))
                if not page.get("has_more"):
                    return items, page.get("next_since") or page.get("generated_at") or s
                s, a = page["next_since"], page["next_after_id"]
            raise ABFError("get_new_bounties: too many pages")

        def via_feed():
            f = self._feed_json()
            key = lambda i: (i.get("first_seen") or "", i["id"])
            items = sorted((i for i in filter_items_locally(f["items"], **filters)
                            if key(i) > (since, after_id or "") and (i.get("first_seen") or "") >= since), key=key)
            return items, f["generated_at"]
        return self._via(via_mcp, via_feed)


# --------------------------------------------------------------------------- triage
_RX_PAID = re.compile(r"(\d+)\s+paid\s*/\s*(\d+)\s+rejected", re.I)
_RX_SUBS = re.compile(r"(\d+)\s+submissions?\s+so\s+far", re.I)
_RX_ATTEMPTS = re.compile(r"(\d+)\s+/attempt\s+comments?", re.I)
_RX_CLAIMS = re.compile(r"(\d+)\s+claiming\s+PRs?", re.I)


def parse_time(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)  # 3.8-safe fractions
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def signals(item, now=None):
    """Structured signals parsed from the free `eligibility` prose + discovery fields."""
    now = now or datetime.now(timezone.utc)
    el = item.get("eligibility") or ""
    s = {}
    m = _RX_PAID.search(el)
    if m:
        s["paid"], s["rejected"] = int(m.group(1)), int(m.group(2))
    m = _RX_SUBS.search(el)
    if m:
        s["submissions"] = int(m.group(1))
    m = _RX_ATTEMPTS.search(el)
    if m:
        s["attempts"] = int(m.group(1))
    m = _RX_CLAIMS.search(el)
    if m:
        s["claiming_prs"] = int(m.group(1))
    dl = parse_time(item.get("deadline"))
    if dl:
        s["hours_left"] = round((dl - now).total_seconds() / 3600.0, 1)
    if re.search(r"single winner|one winner", el + " " + item.get("title", ""), re.I):
        s["single_winner"] = True
    return s


def odds(item, sig):
    """Rough probability your accepted work is paid. Transparent heuristic, not a promise."""
    if item.get("kind") == "audit_contest":
        return 0.02, "audit pool split among all valid findings"
    if "paid" in sig:
        p, r = sig["paid"], sig["rejected"]
        return (p + 1.0) / (p + r + 2.0), "%d paid / %d rejected so far" % (p, r)
    if "submissions" in sig:
        n = sig["submissions"]
        return 1.0 / (n + 1), "%d competing submissions" % n
    if "attempts" in sig or "claiming_prs" in sig:
        a, c = sig.get("attempts", 0), sig.get("claiming_prs", 0)
        return 1.0 / (1 + c + a / 10.0), "%d /attempt, %d claiming PRs" % (a, c)
    if sig.get("single_winner"):
        return 0.1, "single winner, entrant count not in free feed"
    return 0.5, "no competition data in free feed"


def red_flags(item, sig):
    flags = []
    el = (item.get("eligibility") or "")
    req = set(item.get("requires") or [])
    if "purchase" in req or el.upper().startswith("OUTLAY"):
        flags.append("costs you money first")
    if req & {"stake", "bond"}:
        flags.append("stake/bond at risk")
    if "human_only" in req:
        flags.append("human-only")
    if sig.get("paid") == 0 and sig.get("rejected", 0) >= 5:
        flags.append("nobody paid yet (0/%d)" % sig["rejected"])
    if "hours_left" in sig and sig["hours_left"] < 0:
        flags.append("deadline passed")
    elif "hours_left" in sig and sig["hours_left"] < 2:
        flags.append("<2h left")
    if el.lower().startswith("caveat"):
        flags.append("caveat in terms")
    if item.get("est_usd") is None:
        flags.append("USD value unknown")
    return flags


def triage(items, have=None, now=None, include_flagged=False):
    """Annotate, filter by what the agent has, rank by odds-adjusted value."""
    have = set(have) if have is not None else None
    out = []
    for it in items:
        req = set(it.get("requires") or [])
        missing = sorted(req - have) if have is not None else []
        sig = signals(it, now)
        p, why = odds(it, sig)
        flags = red_flags(it, sig)
        row = dict(it)
        row["scout"] = {"missing": missing, "signals": sig, "odds": round(p, 4), "odds_basis": why,
                        "expected_usd": round((it.get("est_usd") or 0) * p, 3), "red_flags": flags}
        if missing:
            continue
        if flags and not include_flagged:
            continue
        out.append(row)
    out.sort(key=lambda r: (-r["scout"]["expected_usd"], -(r.get("est_usd") or 0), r["id"]))
    return out


# --------------------------------------------------------------------------- output
def _money(it):
    if it.get("est_usd") is not None:
        return "$%g" % it["est_usd"]
    return "%s %s" % (it.get("reward"), it.get("currency") or "")


def print_items(items, show_scout=False):
    if not items:
        print("(no matching bounties)")
        return
    for i, it in enumerate(items, 1):
        line = "%2d. %-8s %s" % (i, _money(it), it.get("title", "")[:72])
        print(line)
        meta = "    id=%s  source=%s  requires=%s" % (it["id"], it.get("source"), ",".join(it.get("requires") or []) or "-")
        if it.get("deadline"):
            meta += "  deadline=%s" % it["deadline"][:16]
        if it.get("slots_left") is not None:
            meta += "  slots=%s" % it["slots_left"]
        print(meta)
        if show_scout:
            sc = it["scout"]
            print("    odds~%d%% (%s)  expected~$%.2f%s" % (
                round(sc["odds"] * 100), sc["odds_basis"], sc["expected_usd"],
                ("  FLAGS: " + "; ".join(sc["red_flags"])) if sc["red_flags"] else ""))
        print("    %s" % it.get("url"))


def emit(obj, as_json, printer):
    if as_json:
        json.dump(obj, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        printer(obj)


# --------------------------------------------------------------------------- commands
def cmd_scout(feed, a):
    exclude = a.exclude
    res = feed.list(limit=100, sort="reward", source=a.source, min_usd=a.min_usd, network=a.network,
                    exclude_requires=exclude, q=a.q)
    have = _csv(a.have) if a.have is not None else None
    ranked = triage(res.get("items", []), have=have, include_flagged=a.all)[:a.limit]
    out = {"via": feed.used, "generated_at": res.get("generated_at"), "have": have,
           "count": len(ranked), "items": ranked}

    def show(o):
        print("Agent Bounty Feed scout via %s, feed generated %s" % (o["via"], o["generated_at"]))
        print("You have: %s%s" % (",".join(have) if have is not None else "(not set: showing everything)",
                                  "" if a.all else "  |  hiding red-flagged items (use --all to show)"))
        print("Ranked by expected value = est_usd x rough odds parsed from the free feed.\n")
        print_items(o["items"], show_scout=True)
        print("\nAlways read the terms on the source board before working.")
    emit(out, a.json, show)


def cmd_list(feed, a):
    res = feed.list(limit=a.limit, sort=a.sort, source=a.source, min_usd=a.min_usd, network=a.network,
                    exclude_requires=a.exclude, q=a.q)
    res = dict(res, via=feed.used)
    res.pop("note", None)

    def show(o):
        print("%s matching, showing %d (via %s, feed generated %s)\n" % (
            o.get("total_matching", len(o["items"])), len(o["items"]), o["via"], o.get("generated_at")))
        print_items(o["items"])
    emit(res, a.json, show)


def cmd_get(feed, a):
    it = feed.get(a.id)
    it = triage([it], include_flagged=True)[0]

    def show(o):
        print_items([o], show_scout=True)
        print("    eligibility: %s" % o.get("eligibility"))
    emit(it, a.json, show)


def cmd_stats(feed, a):
    st = feed.stats()

    def show(o):
        print("Feed generated %s (via %s)" % (o.get("generated_at"), feed.used))
        o = o.get("stats") or {}
        print("Open bounties: %s, total ~$%s, new in 24h: %s, no bond/stake/KYC: %s" % (
            o.get("items"), o.get("total_usd"), o.get("new_last_24h"), o.get("no_bond_no_stake_no_kyc")))
        for src, v in sorted((o.get("by_source") or {}).items(), key=lambda kv: -kv[1].get("usd", 0)):
            if v.get("items"):
                print("  %-14s %3d items  $%s" % (src, v["items"], v.get("usd")))
    emit(st, a.json, show)


def cmd_new(feed, a):
    state = {}
    if a.state and os.path.exists(a.state):
        with open(a.state) as fh:
            state = json.load(fh)
    since = a.since or state.get("next_since") or "1970-01-01T00:00:00Z"
    items, next_since = feed.new_since(since, source=a.source, min_usd=a.min_usd, network=a.network,
                                       exclude_requires=a.exclude, q=a.q)
    if a.state:
        tmp = a.state + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"next_since": next_since}, fh)
        os.replace(tmp, a.state)
    out = {"via": feed.used, "since": since, "next_since": next_since, "count": len(items), "items": items}

    def show(o):
        print("%d bounties first seen after %s (via %s). Next poll from %s\n" % (
            o["count"], o["since"], o["via"], o["next_since"]))
        print_items(o["items"])
    emit(out, a.json, show)


def cmd_tools(feed, a):
    tools = feed.mcp.list_tools()
    emit({"server": feed.mcp.server_info, "tools": tools}, a.json,
         lambda o: [print("%-18s %s" % (t["name"], t.get("title", ""))) for t in o["tools"]])


def cmd_doctor(feed, a):
    checks = []

    def check(name, fn):
        try:
            detail = fn()
            checks.append({"check": name, "ok": True, "detail": detail})
        except Exception as e:  # report every failure, keep going
            checks.append({"check": name, "ok": False, "detail": "%s: %s" % (type(e).__name__, e)})

    m = MCPClient()
    check("mcp initialize", lambda: "%s %s" % (m.initialize().get("name"), m.server_info.get("version")))
    check("mcp tools/list", lambda: ",".join(t["name"] for t in m.list_tools()))
    check("mcp list_bounties", lambda: "%d items" % len(m.call("list_bounties", {"limit": 3})["items"]))
    check("feed.json", lambda: "%s items, generated %s" % (http_get_json(FEED_URL)["count"],
                                                           http_get_json(FEED_URL)["generated_at"]))
    check("/earn-now", lambda: "%s items" % http_get_json(EARN_NOW_URL)["count"])
    ok = all(c["ok"] for c in checks)
    emit({"ok": ok, "checks": checks}, a.json,
         lambda o: [print("%s %-18s %s" % ("OK  " if c["ok"] else "FAIL", c["check"], c["detail"])) for c in o["checks"]])
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(prog="abf_scout.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    def common(sp, limit=10):
        sp.add_argument("--json", action="store_true", help="machine-readable JSON output")
        sp.add_argument("--transport", choices=("auto", "mcp", "feed"), default="auto",
                        help="auto = MCP, falling back to feed.json (default)")

    def filters(sp):
        sp.add_argument("--source", help="csv of boards, e.g. taskmarket,direct")
        sp.add_argument("--min-usd", type=float, dest="min_usd")
        sp.add_argument("--network", help="payout network prefix, e.g. base")
        sp.add_argument("--exclude", help="csv of requires[] to drop server-side, e.g. kyc,stake,bond,purchase")
        sp.add_argument("--q", help="text search in title + eligibility")

    s = sub.add_parser("scout", help="bounties you can do, ranked by odds-adjusted value")
    common(s); filters(s)
    s.add_argument("--have", help="csv of requires[] your agent CAN meet, e.g. agent_account,wallet_evm,github")
    s.add_argument("--all", action="store_true", help="also show red-flagged items")
    s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("list", help="list_bounties (raw, server-side filters)")
    common(s); filters(s)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--sort", choices=("reward", "first_seen"), default="reward")
    s = sub.add_parser("get", help="one bounty by id, with parsed signals")
    common(s); s.add_argument("id")
    s = sub.add_parser("new", help="gap-free get_new_bounties; --state makes it cron-safe")
    common(s); filters(s)
    s.add_argument("--since", help="ISO-8601 UTC, e.g. 2026-10-29T00:00:00Z")
    s.add_argument("--state", help="cursor file to read/update, e.g. .abf_cursor.json")
    s = sub.add_parser("stats", help="get_feed_stats"); common(s)
    s = sub.add_parser("tools", help="MCP tools/list"); common(s)
    s = sub.add_parser("doctor", help="check MCP, feed.json and /earn-now are reachable"); common(s)
    return p


def main(argv=None):
    p = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = ("scout", "list", "get", "new", "stats", "tools", "doctor")
    if not any(x in commands or x in ("-h", "--help", "--version") for x in argv[:1]):
        argv = ["scout"] + argv  # bare `abf_scout.py [--have ...]` means scout
    a = p.parse_args(argv)
    if getattr(a, "have", None):
        unknown = set(_csv(a.have)) - set(REQUIRES_VOCAB)
        if unknown:
            p.error("unknown --have value(s): %s (valid: %s)" % (",".join(sorted(unknown)), ",".join(REQUIRES_VOCAB)))
    feed = Feed(transport=a.transport)
    fn = {"scout": cmd_scout, "list": cmd_list, "get": cmd_get, "stats": cmd_stats,
          "new": cmd_new, "tools": cmd_tools, "doctor": cmd_doctor}[a.cmd]
    try:
        return fn(feed, a) or 0
    except (ABFError, urllib.error.URLError, OSError) as e:
        sys.stderr.write("abf_scout: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
