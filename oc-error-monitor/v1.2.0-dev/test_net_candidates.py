#!/usr/bin/env python3
"""Regression tests for the candidate table and the per-node probe.

Two defects these lock down, both found on the live box:

1. The dashboard listed the first 80 members of a 218 member group, so every
   fast node sat below the cut: the best line measured 385ms was at index
   102, the 2nd best at 171. The table showed nodes that could not answer at
   all and hid the ones worth switching to.

2. The measurement used mihomo's group delay endpoint, which answers only for
   the members that replied. A member that times out is left out of the
   response instead of being reported as a timeout, so "dead" and "never
   tested" were the same thing. Measured 48, 52, 58, 60 and 62 answers for
   the same 218 member group across five runs.

So the probe now asks mihomo about one node at a time, and every candidate
ends with a verdict: a delay, or a timeout.
"""

from __future__ import annotations

import sys
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retry_proxy


GROUP = "\U0001F3AF OpenCode\u4e13\u7528"
NESTED = "\U0001F680 \u8282\u70b9\u9009\u62e9"


def _member_names(count=218):
    """A group shaped like the real one: a nested group, DIRECT, then nodes."""
    names = [NESTED, "DIRECT"]
    names += ["\u4f18\u9009\u57df\u540d-%02d" % (i + 1) for i in range(80)]
    names += ["\u591a\u7ebf-%02d" % (i + 1) for i in range(count - len(names))]
    return names


class FakeMihomo:
    """A stand-in for the mihomo controller, and a record of what it was asked."""

    def __init__(self, names, now, delays=None, dead=()):
        self.names = list(names)
        self.now = now
        self.delays = delays or {}
        self.dead = set(dead)
        self.calls = []

    def __call__(self, path, secret=None, timeout=None):
        self.calls.append(path)
        if path == "/proxies":
            proxies = {n: {"type": "Vless", "history": []} for n in self.names}
            proxies[NESTED] = {"type": "Selector", "now": self.names[3], "all": self.names}
            proxies["DIRECT"] = {"type": "Direct"}
            proxies[GROUP] = {
                "type": "Selector",
                "now": self.now,
                "all": list(self.names),
            }
            return {"proxies": proxies}
        if path == "/configs":
            return {"mode": "rule"}
        if path == "/rules":
            return {
                "rules": [
                    {
                        "type": "DOMAIN-SUFFIX",
                        "payload": "opencode.ai",
                        "proxy": GROUP,
                    }
                ]
            }
        if path == "/connections":
            return {"connections": []}
        if path.startswith("/proxies/") and "/delay" in path:
            name = urllib.parse.unquote(
                path.split("/proxies/", 1)[1].split("/delay", 1)[0]
            )
            if name in self.dead:
                raise urllib.error.HTTPError(path, 504, "Timeout", {}, None)
            return {"delay": self.delays.get(name, 380)}
        raise AssertionError("unexpected controller path: %s" % path)


class NetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.names = _member_names()
        self.now = "\u591a\u7ebf-06"
        self.fake = FakeMihomo(self.names, self.now)
        self._orig_mihomo = retry_proxy._mihomo
        self._orig_secret = retry_proxy._clash_secret
        retry_proxy._mihomo = self.fake
        retry_proxy._clash_secret = lambda: "secret"
        # The snapshot is cached for a few seconds; every test needs it fresh.
        retry_proxy._net_cache["at"] = 0.0
        retry_proxy._net_cache["data"] = None
        self._reset_probe()

    def tearDown(self) -> None:
        retry_proxy._mihomo = self._orig_mihomo
        retry_proxy._clash_secret = self._orig_secret
        self._reset_probe()

    def _reset_probe(self) -> None:
        with retry_proxy._probe_lock:
            retry_proxy._probe_state.update(
                running=False, group=None, timeout_ms=5000, total=0, done=0,
                alive=0, dead=0, started_at=0.0, finished_at=0.0, nodes={},
            )


class FamilyTests(NetTestCase):
    def test_family_is_the_name_without_its_number(self) -> None:
        self.assertEqual(retry_proxy._node_family("\u4f18\u9009\u57df\u540d-01"), "\u4f18\u9009\u57df\u540d")
        self.assertEqual(retry_proxy._node_family("\u591a\u7ebf-06"), "\u591a\u7ebf")
        self.assertEqual(retry_proxy._node_family("IPv6\u4f18\u9009-12"), "IPv6\u4f18\u9009")
        self.assertEqual(retry_proxy._node_family("DIRECT"), "DIRECT")

    def test_family_drops_the_leading_emoji(self) -> None:
        self.assertEqual(retry_proxy._node_family(NESTED), "\u8282\u70b9\u9009\u62e9")


class SnapshotTests(NetTestCase):
    def test_every_member_of_the_group_is_listed(self) -> None:
        """The cut at 80 hid every fast node; nothing may be dropped now."""
        snap = retry_proxy.net_snapshot()
        listed = [m["name"] for m in snap["members"]]
        self.assertEqual(len(listed), len(self.names))
        self.assertEqual(len(listed), 218)
        self.assertEqual(snap["candidates"], len(self.names))
        for name in self.names:
            self.assertIn(name, listed)

    def test_the_nodes_that_used_to_be_below_the_cut_are_present(self) -> None:
        snap = retry_proxy.net_snapshot()
        listed = {m["name"] for m in snap["members"]}
        # index 102 and 171 of the real group held the two fastest nodes
        self.assertIn(self.names[102], listed)
        self.assertIn(self.names[171], listed)

    def test_a_group_and_direct_are_marked_unprobeable(self) -> None:
        snap = retry_proxy.net_snapshot()
        by_name = {m["name"]: m for m in snap["members"]}
        self.assertFalse(by_name[NESTED]["probeable"])
        self.assertFalse(by_name["DIRECT"]["probeable"])
        self.assertTrue(by_name[self.now]["probeable"])

    def test_each_member_carries_a_family(self) -> None:
        snap = retry_proxy.net_snapshot()
        by_name = {m["name"]: m for m in snap["members"]}
        self.assertEqual(by_name["\u591a\u7ebf-06"]["family"], "\u591a\u7ebf")
        self.assertEqual(by_name["\u4f18\u9009\u57df\u540d-01"]["family"], "\u4f18\u9009\u57df\u540d")


class ProbeTests(NetTestCase):
    def _run_probe(self, dead=(), timeout_ms=5000):
        self.fake.dead = set(dead)
        started = retry_proxy.net_probe(timeout_ms=timeout_ms, start=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            with retry_proxy._probe_lock:
                if not retry_proxy._probe_state["running"]:
                    break
            time.sleep(0.02)
        with retry_proxy._probe_lock:
            self.assertFalse(retry_proxy._probe_state["running"], "probe never finished")
        return started, retry_proxy.net_probe(timeout_ms=timeout_ms, start=False)

    def test_every_leaf_candidate_ends_with_a_verdict(self) -> None:
        dead = self.names[5:9]
        _, report = self._run_probe(dead=dead)
        leaves = [n for n in self.names if n not in (NESTED, "DIRECT")]
        self.assertEqual(report["tested"], len(leaves))
        self.assertEqual(report["alive"] + report["dead"], len(leaves))
        self.assertEqual(report["dead"], len(dead))

    def test_a_node_that_times_out_is_dead_not_missing(self) -> None:
        dead = self.names[5:9]
        _, report = self._run_probe(dead=dead)
        by_name = {n["name"]: n for n in report["nodes"]}
        for name in dead:
            self.assertIn(name, by_name, "a timed out node must still be reported")
            self.assertTrue(by_name[name]["dead"])
            self.assertIsNone(by_name[name]["delay"])
            self.assertEqual(by_name[name]["error"], "timeout")

    def test_a_working_node_reports_its_delay(self) -> None:
        self.fake.delays[self.now] = 385
        _, report = self._run_probe()
        by_name = {n["name"]: n for n in report["nodes"]}
        self.assertEqual(by_name[self.now]["delay"], 385)
        self.assertFalse(by_name[self.now]["dead"])
        self.assertTrue(by_name[self.now]["current"])

    def test_the_group_delay_endpoint_is_not_used(self) -> None:
        """It omits the members that timed out, so it cannot count them."""
        self._run_probe()
        self.assertEqual([c for c in self.fake.calls if c.startswith("/group/")], [])

    def test_a_report_without_a_run_says_so_instead_of_inventing_numbers(self) -> None:
        report = retry_proxy.net_probe(start=False)
        self.assertTrue(report["ok"])
        self.assertEqual(report["tested"], 0)
        self.assertEqual(report["nodes"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
