"""Tests for abf_scout. Offline by default; set ABF_LIVE=1 to also hit the live feed.

    python3 -m unittest -v test_abf_scout.py
    ABF_LIVE=1 python3 -m unittest -v test_abf_scout.py
"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timezone

import abf_scout as A

NOW = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)

ITEMS = [
    {"id": "frantic:142", "source": "frantic", "kind": "bounty", "title": "Approve a campaign", "est_usd": 3,
     "reward": 3, "currency": "USD", "network": "base (USDC)", "deadline": None, "url": "https://x/142",
     "requires": ["agent_account", "wallet_evm"], "slots_left": 44, "first_seen": "2026-10-23T10:57:31Z",
     "eligibility": "Enlist. Paid in USDC. So far 20 paid / 54 rejected."},
    {"id": "frantic:128", "source": "frantic", "kind": "bounty", "title": "Earn a citation", "est_usd": 8,
     "network": "base (USDC)", "deadline": None, "url": "https://x/128", "requires": ["agent_account", "wallet_evm"],
     "first_seen": "2026-10-23T10:57:31Z", "eligibility": "Enlist. So far 0 paid / 69 rejected."},
    {"id": "frantic:97", "source": "frantic", "kind": "bounty", "title": "On the house", "est_usd": 10,
     "network": "base (USDC)", "url": "https://x/97", "requires": ["agent_account", "purchase", "wallet_evm"],
     "first_seen": "2026-10-23T10:57:31Z", "eligibility": "OUTLAY: you must first fund a bounty. So far 0 paid / 6 rejected."},
    {"id": "taskmarket:TSK-1", "source": "taskmarket", "kind": "bounty", "title": "Cold glass", "est_usd": 2,
     "network": "base", "deadline": "2026-10-29T22:46:20.773Z", "url": "https://x/t1",
     "requires": ["agent_account", "wallet_evm"], "first_seen": "2026-10-28T22:51:13Z",
     "eligibility": "mode=bounty; 137 submissions so far; reward escrowed."},
    {"id": "algora:o/r#1", "source": "algora", "kind": "bounty", "title": "Fix bug", "est_usd": 50,
     "network": "usd", "url": "https://x/a1", "requires": ["github", "kyc"], "first_seen": "2026-10-24T00:10:51Z",
     "eligibility": "Algora bounty. 52 /attempt comments, 0 claiming PRs so far."},
    {"id": "cantina:1", "source": "cantina", "kind": "audit_contest", "title": "Audit", "est_usd": 50000,
     "network": "usd", "url": "https://x/c", "requires": ["agent_account", "github", "kyc"],
     "first_seen": "2026-10-29T01:00:00Z", "eligibility": "Pool split."},
    {"id": "direct:guide", "source": "direct", "kind": "bounty", "title": "Guide, single winner", "est_usd": 5,
     "network": "base", "deadline": "2026-11-12T12:05:00Z", "url": "https://x/d",
     "requires": ["agent_account", "public_post", "wallet_evm"], "first_seen": "2026-10-29T12:07:27Z",
     "eligibility": "Single winner. Make ONE of these."},
]


def rpc_result(req, result):
    return {"jsonrpc": "2.0", "id": req["id"], "result": result}


class FakeServer:
    """Stands in for http_post_json; records calls; can answer as JSON or SSE."""

    def __init__(self, sse=False, session="sess-1", pages=None):
        self.sse, self.session, self.calls, self.pages = sse, session, [], pages or []

    def __call__(self, url, msg, headers, timeout):
        self.calls.append((msg, dict(headers)))
        if "id" not in msg:
            return "application/json", None, ""
        m = msg["method"]
        if m == "initialize":
            body = rpc_result(msg, {"protocolVersion": A.PROTOCOL_VERSION, "serverInfo": {"name": "fake", "version": "9"}})
        elif m == "tools/list":
            body = rpc_result(msg, {"tools": [{"name": "list_bounties"}]})
        elif m == "tools/call":
            name, args = msg["params"]["name"], msg["params"]["arguments"]
            if name == "list_bounties":
                sc = {"generated_at": "G", "items": ITEMS[:args.get("limit", 20)], "total_matching": len(ITEMS)}
            elif name == "get_new_bounties":
                sc = self.pages.pop(0)
            elif name == "get_bounty":
                if args["id"] == "missing":
                    body = rpc_result(msg, {"content": [{"type": "text", "text": "No open bounty with id missing"}], "isError": True})
                    return self._wrap(body)
                sc = {"generated_at": "G", "item": ITEMS[0]}
            else:
                sc = {"generated_at": "G", "stats": {"items": 7}}
            body = rpc_result(msg, {"content": [{"type": "text", "text": json.dumps(sc)}], "structuredContent": sc})
        else:
            body = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}
        return self._wrap(body)

    def _wrap(self, body):
        if self.sse:
            return "text/event-stream", self.session, "event: message\ndata: %s\n\n" % json.dumps(body)
        return "application/json", self.session, json.dumps(body)


def down(*a, **k):
    raise urllib.error.URLError("offline")


FEED = {"generated_at": "2026-10-29T19:46:57Z", "count": len(ITEMS), "stats": {"items": len(ITEMS)}, "items": ITEMS}


class TestMCPClient(unittest.TestCase):
    def test_json_handshake_session_and_call(self):
        srv = FakeServer()
        c = A.MCPClient(post=srv)
        res = c.call("list_bounties", {"limit": 2})
        self.assertEqual(len(res["items"]), 2)
        methods = [m["method"] for m, _ in srv.calls]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"])
        self.assertNotIn("Mcp-Session-Id", srv.calls[0][1])
        self.assertEqual(srv.calls[2][1]["Mcp-Session-Id"], "sess-1")  # session echoed back
        self.assertEqual(srv.calls[2][1]["MCP-Protocol-Version"], A.PROTOCOL_VERSION)

    def test_sse_reply(self):
        c = A.MCPClient(post=FakeServer(sse=True))
        self.assertEqual(c.list_tools()[0]["name"], "list_bounties")

    def test_tool_error_and_rpc_error(self):
        c = A.MCPClient(post=FakeServer())
        with self.assertRaises(A.ABFError):
            c.call("get_bounty", {"id": "missing"})
        with self.assertRaises(A.ABFError):
            c._rpc("bogus/method")

    def test_parse_rpc_body_picks_matching_id(self):
        text = 'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":1}}\n\n'
        self.assertEqual(A.parse_rpc_body("text/event-stream", text, 7)["result"], {"ok": 1})
        with self.assertRaises(A.ABFError):
            A.parse_rpc_body("application/json", '{"jsonrpc":"2.0","id":1,"result":{}}', 2)


class TestFeed(unittest.TestCase):
    def test_new_since_follows_pages(self):
        pages = [{"items": [ITEMS[0]], "has_more": True, "next_since": "S1", "next_after_id": "frantic:142"},
                 {"items": [ITEMS[3]], "has_more": False, "next_since": "S2", "next_after_id": None}]
        srv = FakeServer(pages=pages)
        f = A.Feed(mcp=A.MCPClient(post=srv))
        items, nxt = f.new_since("2026-01-01T00:00:00Z")
        self.assertEqual([i["id"] for i in items], ["frantic:142", "taskmarket:TSK-1"])
        self.assertEqual(nxt, "S2")
        second = [m for m, _ in srv.calls if m.get("method") == "tools/call"][1]["params"]["arguments"]
        self.assertEqual((second["since"], second["after_id"]), ("S1", "frantic:142"))

    def test_fallback_to_feed_json(self):
        f = A.Feed(mcp=A.MCPClient(post=down), get_json=lambda url: FEED)
        res = f.list(limit=100, exclude_requires="kyc,purchase", source=None)
        self.assertEqual(f.used, "feed.json")
        ids = {i["id"] for i in res["items"]}
        self.assertNotIn("algora:o/r#1", ids)
        self.assertNotIn("frantic:97", ids)
        self.assertEqual(f.get("frantic:142")["id"], "frantic:142")
        items, nxt = f.new_since("2026-10-28T00:00:00Z")
        self.assertEqual([i["id"] for i in items], ["taskmarket:TSK-1", "cantina:1", "direct:guide"])
        self.assertEqual(nxt, FEED["generated_at"])

    def test_mcp_only_transport_does_not_fall_back(self):
        f = A.Feed(transport="mcp", mcp=A.MCPClient(post=down), get_json=lambda url: FEED)
        with self.assertRaises(urllib.error.URLError):
            f.list()

    def test_local_filters(self):
        got = A.filter_items_locally(ITEMS, source="frantic,direct", min_usd=4, network="base", q="citation")
        self.assertEqual([i["id"] for i in got], ["frantic:128"])


class TestTriage(unittest.TestCase):
    def test_signals_parsed_from_eligibility(self):
        self.assertEqual(A.signals(ITEMS[0], NOW), {"paid": 20, "rejected": 54})
        self.assertEqual(A.signals(ITEMS[3], NOW), {"submissions": 137, "hours_left": 2.8})
        self.assertEqual(A.signals(ITEMS[4], NOW), {"attempts": 52, "claiming_prs": 0})
        self.assertTrue(A.signals(ITEMS[6], NOW)["single_winner"])

    def test_have_filter_and_red_flags(self):
        ranked = A.triage(ITEMS, have=["agent_account", "wallet_evm"], now=NOW)
        ids = [r["id"] for r in ranked]
        # needs github/kyc/public_post -> missing; 0-paid and OUTLAY -> red-flagged and hidden
        self.assertEqual(ids, ["frantic:142", "taskmarket:TSK-1"])
        allr = {r["id"]: r for r in A.triage(ITEMS, have=None, now=NOW, include_flagged=True)}
        self.assertIn("costs you money first", allr["frantic:97"]["scout"]["red_flags"])
        self.assertIn("nobody paid yet (0/69)", allr["frantic:128"]["scout"]["red_flags"])
        self.assertEqual(allr["cantina:1"]["scout"]["odds"], 0.02)

    def test_ranking_is_expected_value(self):
        ranked = A.triage(ITEMS, have=None, now=NOW)
        evs = [r["scout"]["expected_usd"] for r in ranked]
        self.assertEqual(evs, sorted(evs, reverse=True))
        top = ranked[0]
        self.assertAlmostEqual(top["scout"]["expected_usd"], round(top["est_usd"] * top["scout"]["odds"], 3))

    def test_deadline_flags(self):
        late = dict(ITEMS[3], deadline="2026-10-29T19:00:00Z")
        self.assertIn("deadline passed", A.triage([late], now=NOW, include_flagged=True)[0]["scout"]["red_flags"])
        soon = dict(ITEMS[3], deadline="2026-10-29T21:00:00Z")
        self.assertIn("<2h left", A.triage([soon], now=NOW, include_flagged=True)[0]["scout"]["red_flags"])


class TestCLI(unittest.TestCase):
    def run_cli(self, argv, monkey_feed=True):
        orig = A.Feed
        if monkey_feed:
            A.Feed = lambda transport="auto": orig(transport=transport, mcp=A.MCPClient(post=down), get_json=lambda u: FEED)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = A.main(argv)
        finally:
            A.Feed = orig
        return rc, buf.getvalue()

    def test_bare_invocation_is_scout_json(self):
        rc, out = self.run_cli(["--have", "agent_account,wallet_evm", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["via"], "feed.json")
        self.assertTrue(all(not r["scout"]["missing"] for r in data["items"]))

    def test_new_with_state_file_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "cursor.json")
            rc, out = self.run_cli(["new", "--since", "2026-10-28T00:00:00Z", "--state", st, "--json"])
            self.assertEqual(json.loads(out)["count"], 3)
            rc, out = self.run_cli(["new", "--state", st, "--json"])
            self.assertEqual(json.loads(out)["count"], 0)  # nothing repeats on the next poll

    def test_unknown_have_value_rejected(self):
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
                A.main(["scout", "--have", "wallet"])


@unittest.skipUnless(os.environ.get("ABF_LIVE"), "set ABF_LIVE=1 to run live checks")
class TestLive(unittest.TestCase):
    def test_live_mcp(self):
        c = A.MCPClient()
        self.assertEqual(c.initialize().get("name"), "agent-bounty-feed")
        names = {t["name"] for t in c.list_tools()}
        self.assertTrue({"list_bounties", "get_new_bounties", "get_bounty", "get_feed_stats"} <= names)
        res = c.call("list_bounties", {"limit": 5, "exclude_requires": "kyc,stake,bond"})
        self.assertTrue(res["items"])
        for it in res["items"]:
            self.assertFalse({"kyc", "stake", "bond"} & set(it["requires"]))
        one = A.Feed(mcp=c).get(res["items"][0]["id"])
        self.assertEqual(one["id"], res["items"][0]["id"])

    def test_live_feed_json_and_earn_now(self):
        feed = A.http_get_json(A.FEED_URL)
        self.assertGreater(feed["count"], 0)
        self.assertGreater(A.http_get_json(A.EARN_NOW_URL)["count"], 0)

    def test_live_doctor(self):
        rc, out = TestCLI.run_cli(self, ["doctor", "--json"], monkey_feed=False)
        self.assertEqual(rc, 0, out)


import unittest.mock  # noqa: E402  (used in TestCLI)

if __name__ == "__main__":
    unittest.main()
