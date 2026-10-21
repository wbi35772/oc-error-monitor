#!/usr/bin/env python3
"""Regression tests for the route ledger, health and strategies (L1-L3).

Three defects these lock down, all found by sending one real request through
the dev proxy:

1. `pick_route` chose a member in its strategy branch and never wrote the
   choice back to the ledger. Sticky rotation could therefore never engage:
   every attempt re-ran the strategy, and because the failed member had just
   dropped down the ranking the proxy moved to a fresh node every attempt.
   It looked like deliberate rotation and was actually stateless re-picking,
   with `switches` stuck at 0 so nothing reported it.

2. The ledger was fed the log vocabulary. `classify()` says `ERR:URLError`;
   `controller.SWITCH_CLASSES` says `NET_*`. Nothing overlapped, so the four
   dead IPv6 members the request walked through were never cooled down and
   never skipped. The one class that most needs a rotation caused none.

3. With no data yet, every member ties, and `DIRECT` sorts before the emoji
   names, so a neutral DIRECT won every tie and quietly bypassed the pool.

Run: python test_route_policy.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retry_proxy


def _pool(names, now="n1", group_type="Selector", ok=True):
    members = []
    for name in names:
        members.append({
            "name": name,
            "type": "Direct" if name == "DIRECT" else "Vless",
            "family": "DIRECT" if name == "DIRECT" else name.rstrip("0123456789-"),
            "probeable": name != "DIRECT",
            "history_delay": None,
        })
    return {
        "ok": ok,
        "group": "G",
        "group_type": group_type,
        "now": now,
        "basis": "test",
        "count": len(members),
        "members": members,
        "ts": "test",
    }


class RoutePolicyBase(unittest.TestCase):
    MEMBERS = ["n1", "n2", "n3", "DIRECT"]

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="ocm-route-")
        self._path = os.path.join(self._dir, "route_state.json")
        self._orig = (
            copy.deepcopy(retry_proxy.ROUTE),
            copy.deepcopy(retry_proxy.SETTINGS),
            retry_proxy.ROUTE_STATE_PATH,
            copy.deepcopy(retry_proxy._route_state),
            list(retry_proxy._route_dirty),
            list(retry_proxy._route_saved_at),
        )
        retry_proxy.ROUTE_STATE_PATH = self._path
        retry_proxy._route_state.update(
            {"nodes": {}, "families": {}, "current": None, "switches": 0, "rr": 0,
             "last_switch": None}
        )
        retry_proxy._route_dirty[0] = False
        retry_proxy.ROUTE["select"].update(
            {"strategy": "least_fail", "pin": None, "threshold": 1,
             "cooldown_s": 120, "family_threshold": 3, "family_cooldown_s": 900,
             "window_s": 600}
        )
        retry_proxy.SETTINGS["force_route"] = None
        self.pool = _pool(self.MEMBERS)
        self.sent = []

    def tearDown(self):
        (route, settings, path, state, dirty, saved) = self._orig
        retry_proxy.ROUTE.clear()
        retry_proxy.ROUTE.update(route)
        retry_proxy.SETTINGS.clear()
        retry_proxy.SETTINGS.update(settings)
        retry_proxy.ROUTE_STATE_PATH = path
        retry_proxy._route_state.clear()
        retry_proxy._route_state.update(state)
        retry_proxy._route_dirty[:] = dirty
        retry_proxy._route_saved_at[:] = saved

    def patch(self, pool=None):
        pool = pool if pool is not None else self.pool

        def fake_send(path, secret, method, payload):
            self.sent.append({"path": path, "method": method, "payload": payload})
            if payload and payload.get("name"):
                pool["now"] = payload["name"]

        return [
            mock.patch.object(retry_proxy, "discover_pool", lambda force=False: pool),
            mock.patch.object(retry_proxy, "_clash_secret", lambda: ""),
            mock.patch.object(retry_proxy, "_mihomo_send", fake_send),
        ]


class Stickiness(RoutePolicyBase):
    def test_adopts_mihomo_current_then_stays(self):
        with self.patch()[0]:
            first = retry_proxy.route_for_attempt()
            self.assertEqual(first["name"], "n1")
            self.assertEqual(first["via"], "adopted")
            for _ in range(3):
                again = retry_proxy.route_for_attempt()
                self.assertEqual(again["name"], "n1")
                self.assertEqual(again["via"], "sticky")

    def test_a_working_member_survives_success_and_failure(self):
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt()
            retry_proxy.report_route(route, "OK", 200, 120)
            retry_proxy.report_route(route, "HTTP403", 403, 90)
            after = retry_proxy.route_for_attempt()
            self.assertEqual(after["name"], "n1")
            self.assertEqual(retry_proxy._route_state["switches"], 0)

    def test_adoption_is_skipped_when_mihomo_sits_on_a_cooled_member(self):
        with self.patch()[0]:
            retry_proxy.report_route({"name": "n1"}, "NET_STREAM", None, 10)
            route = retry_proxy.route_for_attempt()
            self.assertNotEqual(route["name"], "n1")


class Rotation(RoutePolicyBase):
    def test_a_stream_failure_cools_the_member_and_the_next_attempt_moves(self):
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt()
            self.assertEqual(route["name"], "n1")
            retry_proxy.report_route(
                route, "ERR:URLError", None, 900,
                err="<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred>",
            )
            self.assertTrue(retry_proxy.member_health("n1")["cooling"])
            self.assertEqual(retry_proxy._route_state["switches"], 1)

            moved = retry_proxy.route_for_attempt()
            self.assertEqual(moved["name"], "n2")
            self.assertEqual(moved["via"], "strategy:least_fail")

    def test_a_class_outside_rotate_on_records_but_does_not_move(self):
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt()
            retry_proxy.report_route(route, "RATE", 429, 800)
            health = retry_proxy.member_health("n1")
            self.assertFalse(health["cooling"])
            # A decay tick between report and read makes this 0.999999, not 1.0.
            self.assertEqual(set(health["fail_by_class"]), {"RATE_LIMIT"})
            self.assertAlmostEqual(
                health["fail_by_class"]["RATE_LIMIT"], 1.0, places=3
            )
            self.assertEqual(retry_proxy.route_for_attempt()["name"], "n1")

    def test_cooldown_expires(self):
        with self.patch()[0]:
            retry_proxy.report_route({"name": "n1"}, "NET_STREAM", None, 10)
            with retry_proxy._route_lock:
                retry_proxy._route_state["nodes"]["n1"]["cooldown_until"] = 1.0
            self.assertFalse(retry_proxy.member_health("n1")["cooling"])

    def test_everything_cooling_falls_back_to_direct(self):
        pool = _pool(["n1", "n2", "DIRECT"], now=None)
        with self.patch(pool)[0]:
            for name in ("n1", "n2", "DIRECT"):
                retry_proxy.report_route({"name": name}, "NET_STREAM", None, 10)
            route = retry_proxy.route_for_attempt()
            self.assertEqual(route["name"], "DIRECT")


class Precedence(RoutePolicyBase):
    def test_pin_wins(self):
        retry_proxy.ROUTE["select"]["pin"] = "n3"
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt()
            self.assertEqual(route["name"], "n3")
            self.assertEqual(route["via"], "pin")

    def test_force_route_direct_wins_over_strategy(self):
        retry_proxy.SETTINGS["force_route"] = "direct"
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt()
            self.assertEqual(route["name"], "DIRECT")
            self.assertEqual(route["via"], "force_route")

    def test_force_route_proxy_leaves_the_choice_to_mihomo(self):
        retry_proxy.SETTINGS["force_route"] = "proxy"
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt()
            self.assertEqual(route["name"], "n1")
            self.assertEqual(route["via"], "force_route:proxy")
            self.assertEqual(self.sent, [])

    def test_passive_strategy_leaves_the_choice_to_mihomo(self):
        retry_proxy.ROUTE["select"]["strategy"] = "off"
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt()
            self.assertEqual(route["name"], "n1")
            self.assertEqual(route["via"], "mihomo")
            self.assertEqual(self.sent, [])

    def test_locally_refused_request_falls_back_to_direct(self):
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt(force_direct=True)
            self.assertTrue(route["direct"])
            self.assertEqual(route["via"], "local fallback")

    def test_direct_pseudo_member_is_never_chosen_over_an_equal_node(self):
        retry_proxy.ROUTE["select"]["pin"] = None
        pool = _pool(["n1", "DIRECT"], now=None)
        with self.patch(pool)[0]:
            route = retry_proxy.route_for_attempt()
            self.assertEqual(route["name"], "n1")


class Strategies(RoutePolicyBase):
    def test_every_registered_strategy_produces_a_member(self):
        names = [m["name"] for m in self.pool["members"]]
        for strategy in retry_proxy.STRATEGIES:
            with self.subTest(strategy=strategy):
                retry_proxy.ROUTE["select"]["strategy"] = strategy
                retry_proxy.ROUTE["select"]["pin"] = None
                retry_proxy._route_state.update({"current": None, "rr": 0})
                with self.patch()[0]:
                    route = retry_proxy.route_for_attempt()
                self.assertIn(route["name"], names)

    def test_lowest_latency_follows_the_probe(self):
        retry_proxy.ROUTE["select"]["strategy"] = "lowest_latency"
        retry_proxy._route_state["current"] = None
        with mock.patch.object(
            retry_proxy, "_probe_verdict",
            lambda name: (False, {"n1": 900, "n2": 30, "n3": 500}.get(name)),
        ):
            # now=None so the adopt branch cannot win the pick before the
            # strategy is consulted: this test is about the ordering.
            with self.patch(_pool(self.MEMBERS, now=None))[0]:
                route = retry_proxy.route_for_attempt()
        self.assertEqual(route["name"], "n2")

    def test_a_member_the_probe_declared_dead_is_skipped(self):
        retry_proxy._route_state["current"] = None
        with mock.patch.object(
            retry_proxy, "_probe_verdict",
            lambda name: (name == "n1", 10 if name != "n1" else None),
        ):
            with self.patch()[0]:
                route = retry_proxy.route_for_attempt()
        self.assertNotEqual(route["name"], "n1")

    def test_a_stale_probe_verdict_is_ignored(self):
        old = retry_proxy.ROUTE["probe"]["stale_s"]
        retry_proxy.ROUTE["probe"]["stale_s"] = 1
        try:
            with mock.patch.object(retry_proxy, "_probe_state", {
                "finished_at": retry_proxy.time.time() - 600,
                "nodes": {"n1": {"dead": True}},
            }):
                dead, delay = retry_proxy._probe_verdict("n1")
        finally:
            retry_proxy.ROUTE["probe"]["stale_s"] = old
        self.assertIsNone(dead)
        self.assertIsNone(delay)


class PolicyVocabulary(unittest.TestCase):
    CASES = [
        ("OK", None, "OK"),
        ("EMPTY_STREAM", None, "NET_STREAM"),
        ("IP_REP", None, "AUTH_IP_REPUTATION"),
        ("IDENTITY", None, "AUTH_FREE_TIER"),
        ("RATE", None, "RATE_LIMIT"),
        ("5XX", None, "UPSTREAM_5XX"),
        ("CF_UA", None, "CLIENT_REQUEST"),
        ("HTTP403", None, "HTTP403"),
        ("ERR:URLError", "timed out", "NET_TIMEOUT"),
        ("ERR:URLError", "getaddrinfo failed", "NET_DNS"),
        ("ERR:URLError", "Connection refused", "NET_CONNECT"),
        ("ERR:URLError", "certificate verify failed", "NET_TLS"),
        ("ERR:URLError", "[SSL: UNEXPECTED_EOF_WHILE_READING]", "NET_STREAM"),
        ("ERR:ConnectionResetError", "", "NET_CONNECT"),
        ("ERR:WeirdError", "something new", "ERR:WeirdError"),
    ]

    def test_mapping(self):
        for cls, err, expected in self.CASES:
            with self.subTest(cls=cls, err=err):
                self.assertEqual(retry_proxy.policy_class(cls, err), expected)

    def test_only_rotate_on_classes_can_cool_a_member(self):
        """Whatever the mapping produces, it must not smuggle in a class
        controller.py deliberately keeps out of the switching set."""
        import controller

        rotate_on = set(retry_proxy.DEFAULT_ROUTE["rotate_on"])
        for cls, err, _ in self.CASES:
            with self.subTest(cls=cls):
                policy = retry_proxy.policy_class(cls, err)
                if policy in rotate_on:
                    self.assertIn(policy, controller.SWITCH_CLASSES)
                    self.assertNotIn(policy, controller.NO_SWITCH_REASONS)


class LedgerPersistence(RoutePolicyBase):
    def test_ledger_survives_a_reload(self):
        with self.patch()[0]:
            route = retry_proxy.route_for_attempt()
            retry_proxy.report_route(route, "OK", 200, 250)
            retry_proxy._route_save(force=True)
            self.assertEqual(retry_proxy._route_state["current"], "n1")

        raw = json.loads(Path(self._path).read_text(encoding="utf-8"))
        self.assertEqual(raw["current"], "n1")
        self.assertEqual(raw["nodes"]["n1"]["ok"], 1)

        # The member then fails with a rotate_on class: it leaves `current`
        # and picks up a cooldown, and the file has to say so as well.
        with self.patch()[0]:
            retry_proxy.report_route(route, "ERR:URLError", None, 400,
                                     err="[SSL: UNEXPECTED_EOF_WHILE_READING]")
            retry_proxy._route_save(force=True)

        raw = json.loads(Path(self._path).read_text(encoding="utf-8"))
        self.assertIsNone(raw["current"])
        entry = raw["nodes"]["n1"]
        self.assertEqual(entry["ok"], 1)
        self.assertEqual(set(entry["fail_by_class"]), {"NET_STREAM"})
        self.assertAlmostEqual(entry["fail_by_class"]["NET_STREAM"], 1.0, places=3)
        self.assertEqual(entry["last_raw_class"], "ERR:URLError")

        before = dict(retry_proxy._route_state["nodes"])
        retry_proxy._route_state["nodes"] = {}
        retry_proxy._route_load()
        self.assertIn("n1", retry_proxy._route_state["nodes"])
        self.assertEqual(
            retry_proxy._route_state["nodes"]["n1"]["ok"], before["n1"]["ok"]
        )

    def test_a_corrupt_ledger_keeps_the_proxy_running(self):
        Path(self._path).write_text("{not json", encoding="utf-8")
        retry_proxy._route_load()
        self.assertEqual(retry_proxy._route_state["nodes"], {})

    def test_weights_decay_with_the_half_life(self):
        retry_proxy.ROUTE["select"]["decay_half_life_s"] = 3600
        entry = {"fail": 8.0, "fail_by_class": {"NET_STREAM": 8.0}, "decay_ts": 0.0}
        retry_proxy._decay(entry, now=3600.0)
        self.assertAlmostEqual(entry["fail"], 4.0, places=6)
        self.assertAlmostEqual(entry["fail_by_class"]["NET_STREAM"], 4.0, places=6)


class FamilyCooldown(RoutePolicyBase):
    """A whole family answers or fails together.

    Measured on the live box: one request walked IPv6优选-01 → -02 → -03 → -04,
    four dead attempts, because each member looked fine until it was tried.
    """

    FAMILY = ["f-01", "f-02", "f-03", "g-01", "g-02", "DIRECT"]
    SSL_EOF = "<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred>"

    def _fail(self, name):
        retry_proxy.report_route({"name": name}, "ERR:URLError", None, 900, err=self.SSL_EOF)

    def _names(self, pool):
        return [m["name"] for m in retry_proxy._healthy_members(pool)]

    def test_one_member_does_not_condemn_its_family(self):
        self._fail("f-01")
        health = retry_proxy.member_health("f-01")
        self.assertEqual(health["family"], "f")
        self.assertFalse(health["family_cooling"])
        self.assertEqual(health["family_recent_fails"], 1)

        names = self._names(_pool(self.FAMILY, now=None))
        self.assertIn("f-02", names)
        self.assertIn("f-03", names)

    def test_three_members_condemn_the_family(self):
        for name in ("f-01", "f-02", "f-03"):
            self._fail(name)
        health = retry_proxy.member_health("f-03")
        self.assertTrue(health["family_cooling"])
        self.assertEqual(health["family_recent_fails"], 3)

        names = self._names(_pool(self.FAMILY, now=None))
        self.assertEqual(names, ["g-01", "g-02", "DIRECT"])

    def test_the_threshold_is_configurable(self):
        retry_proxy.ROUTE["select"]["family_threshold"] = 2
        self._fail("f-01")
        self._fail("f-02")
        self.assertTrue(retry_proxy.member_health("f-01")["family_cooling"])

    def test_family_cooldown_expires(self):
        for name in ("f-01", "f-02", "f-03"):
            self._fail(name)
        self.assertTrue(retry_proxy.member_health("f-01")["family_cooling"])

        retry_proxy._route_state["families"]["f"]["cooldown_until"] = 1.0
        # The members are still cooling on their own account; this test is
        # about the family lever, so clear them too.
        for entry in retry_proxy._route_state["nodes"].values():
            entry["cooldown_until"] = 1.0
        self.assertFalse(retry_proxy.member_health("f-01")["family_cooling"])
        self.assertIn("f-01", self._names(_pool(self.FAMILY, now=None)))

    def test_a_lone_node_is_never_family_cooled(self):
        pool = _pool(["solo", "DIRECT"], now=None)
        for _ in range(4):
            self._fail("solo")
        health = retry_proxy.member_health("solo")
        self.assertEqual(health["family"], "solo")
        self.assertFalse(health["family_cooling"])
        self.assertNotIn("solo", retry_proxy._route_state["families"])

        # The member cooled itself down, which is correct and separate. Clear
        # that and it is a usable candidate again: no family lever was pulled.
        retry_proxy._route_state["nodes"]["solo"]["cooldown_until"] = 1.0
        self.assertIn("solo", self._names(pool))

    def test_failures_outside_the_window_are_not_counted(self):
        self._fail("f-01")
        self._fail("f-02")
        self.assertEqual(retry_proxy._family_snapshot(time.time())["f"]["recent"], 2)

        for entry in retry_proxy._route_state["nodes"].values():
            entry["last_fail_ts"] = 1.0
        row = retry_proxy._family_snapshot(time.time())["f"]
        self.assertEqual(row["recent"], 0)
        self.assertFalse(row["cooling"])

    def test_snapshot_covers_every_family_with_failures(self):
        self._fail("f-01")
        self._fail("g-01")
        snap = retry_proxy._family_snapshot(time.time())
        self.assertEqual(set(snap), {"f", "g"})
        self.assertEqual(snap["g"]["recent"], 1)
        self.assertNotIn("DIRECT", snap)

    def test_clear_drops_families_too(self):
        for name in ("f-01", "f-02", "f-03"):
            self._fail(name)
        self.assertTrue(retry_proxy._route_state["families"])

        with mock.patch.object(retry_proxy, "save_settings", lambda reason="": (True, None)):
            with self.patch()[0]:
                out = retry_proxy.route_action(b'{"action":"clear"}')
        self.assertTrue(out["ok"])
        self.assertEqual(retry_proxy._route_state["families"], {})
        self.assertEqual(retry_proxy._route_state["nodes"], {})

    def test_the_switch_record_names_the_family(self):
        for name in ("f-01", "f-02", "f-03"):
            self._fail(name)
        last = retry_proxy._route_state["last_switch"]
        self.assertEqual(last["family"], "f")
        self.assertTrue(last["family_cooled"])

    def test_route_status_reports_the_families(self):
        for name in ("f-01", "f-02", "f-03"):
            self._fail(name)
        with self.patch()[0]:
            out = retry_proxy.route_status()
        self.assertEqual(out["family_threshold"], 3)
        self.assertEqual(out["family_cooldown_s"], 900)
        names = [row["name"] for row in out["families"]]
        self.assertIn("f", names)
        row = [r for r in out["families"] if r["name"] == "f"][0]
        self.assertGreater(row["cooldown_s"], 0)
        self.assertTrue(row["cooling"])

    def test_route_status_names_the_state_files(self):
        with self.patch()[0]:
            out = retry_proxy.route_status()
        self.assertEqual(out["settings_path"], retry_proxy.SETTINGS_PATH)
        self.assertEqual(out["state_path"], retry_proxy.ROUTE_STATE_PATH)
        self.assertEqual(out["settings_schema"], retry_proxy.SETTINGS_SCHEMA)


class RouteAction(RoutePolicyBase):
    def _post(self, body):
        with mock.patch.object(
            retry_proxy, "save_settings", lambda reason="": (True, None)
        ):
            with self.patch()[0]:
                return retry_proxy.route_action(body)

    def test_set_changes_the_knobs(self):
        out = self._post(
            b'{"action":"set","threshold":2,"cooldown_s":180,'
            b'"family_threshold":4,"family_cooldown_s":1200}'
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["changed"]["threshold"], 2)
        self.assertEqual(out["changed"]["cooldown_s"], 180)
        self.assertEqual(out["changed"]["family_threshold"], 4)
        self.assertEqual(out["changed"]["family_cooldown_s"], 1200)
        self.assertEqual(retry_proxy.ROUTE["select"]["threshold"], 2)

    def test_set_clamps_out_of_range_values(self):
        out = self._post(b'{"action":"set","threshold":9999,"cooldown_s":-5}')
        self.assertTrue(out["ok"])
        self.assertEqual(out["changed"]["threshold"], 10)
        self.assertEqual(out["changed"]["cooldown_s"], 5)

    def test_set_ignores_nonsense(self):
        out = self._post(b'{"action":"set","threshold":"soon"}')
        self.assertTrue(out["ok"])
        self.assertEqual(out["changed"], {})

    def test_unknown_strategy_is_refused(self):
        out = self._post(b'{"action":"strategy","value":"nonsense"}')
        self.assertFalse(out["ok"])
        self.assertIn("unknown strategy", out["reason"])
        self.assertIn("least_fail", out["known"])

    def test_every_registered_strategy_is_accepted(self):
        for strategy in list(retry_proxy.STRATEGIES) + list(
            retry_proxy.PASSIVE_STRATEGIES
        ):
            with self.subTest(strategy=strategy):
                out = self._post(
                    ('{"action":"strategy","value":"%s"}' % strategy).encode()
                )
                self.assertTrue(out["ok"], out)
                self.assertEqual(out["strategy"], strategy)

    def test_pin_and_clear(self):
        out = self._post(b'{"action":"pin","name":"f-01"}')
        self.assertTrue(out["ok"])
        self.assertEqual(retry_proxy.ROUTE["select"]["pin"], "f-01")

        out = self._post(b'{"action":"pin","name":""}')
        self.assertTrue(out["ok"])
        self.assertIsNone(retry_proxy.ROUTE["select"]["pin"])

    def test_unknown_action_is_refused(self):
        out = self._post(b'{"action":"dance"}')
        self.assertFalse(out["ok"])
        self.assertIn("unknown action", out["reason"])

    def test_bad_json_is_refused(self):
        out = self._post(b'{not json')
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "bad json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
