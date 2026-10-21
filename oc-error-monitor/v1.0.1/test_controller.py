#!/usr/bin/env python3
"""Unit tests for the P2 controller policy. No network access required."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import controller


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_000_000.0
        self.state = {
            "switch_times": [],
            "class_last_action": {},
            "last_switch_ts": 0.0,
        }

    def decide(self, error_class: str, **kwargs):
        event = {"error_class": error_class}
        defaults = dict(
            now=self.now,
            min_switch_interval=120,
            class_cooldown=300,
            max_per_hour=3,
            max_per_15min=2,
        )
        defaults.update(kwargs)
        return controller.decide(event, self.state, **defaults)

    def test_auth_classes_never_switch(self) -> None:
        for cls in (
            "AUTH_FREE_TIER",
            "AUTH_CREDENTIAL",
            "NET_TLS",
            "RATE_LIMIT",
            "UPSTREAM_5XX",
            "LOCAL_RELAY",
            "CLIENT_REQUEST",
            "ABORTED",
            "UNKNOWN",
        ):
            with self.subTest(cls=cls):
                self.assertEqual(self.decide(cls).kind, "NOOP")

    def test_ip_reputation_is_left_to_the_per_session_layer(self) -> None:
        """A 403 must not move the shared node; P3 switches that one session.

        Backtest (backtest.py, 2.1 days / 135 sessions / 150 errors):
        51 of the 52 sessions that ended on a 403 had exactly one such
        error, and 46 of them were followed by a new session within five
        minutes - the user gave up and restarted. A global node switch
        would move every session's exit IP to fix one session's problem,
        and a model switch fixes it at a measured 100% vs 33%-83%.
        """
        decision = self.decide("AUTH_IP_REPUTATION")
        self.assertEqual(decision.kind, "NOOP")
        self.assertIn("session_failover", decision.reason)

    def test_network_class_can_switch(self) -> None:
        for cls in ("NET_DNS", "NET_CONNECT", "NET_STREAM", "NET_TIMEOUT"):
            with self.subTest(cls=cls):
                self.assertEqual(self.decide(cls).kind, "SWITCH")

    def test_min_interval_blocks(self) -> None:
        self.state["last_switch_ts"] = self.now - 30
        self.assertEqual(self.decide("NET_DNS").kind, "SKIP")

    def test_class_cooldown_blocks(self) -> None:
        self.state["class_last_action"] = {"NET_DNS": self.now - 60}
        self.assertEqual(self.decide("NET_DNS").kind, "SKIP")
        self.assertEqual(self.decide("NET_CONNECT").kind, "SWITCH")

    def test_hourly_budget_blocks(self) -> None:
        self.state["switch_times"] = [self.now - 10, self.now - 20, self.now - 30]
        self.assertEqual(self.decide("NET_DNS").kind, "SKIP")

    def test_15min_budget_blocks(self) -> None:
        self.state["switch_times"] = [self.now - 10, self.now - 20]
        self.assertEqual(self.decide("NET_DNS").kind, "SKIP")


class CandidateTests(unittest.TestCase):
    def test_filters_and_rotates(self) -> None:
        group = {
            "now": "B",
            "all": [
                "DIRECT",
                "Info 剩余流量",
                "A",
                "B",
                "C",
                "GroupChild",
                "Dead",
                "D",
            ],
        }
        proxies = {
            "DIRECT": {"type": "Direct"},
            "Info 剩余流量": {"type": "Shadowsocks"},
            "A": {"type": "Shadowsocks", "alive": True},
            "B": {"type": "Shadowsocks", "alive": True},
            "C": {"type": "Vmess", "alive": True},
            "GroupChild": {"type": "Selector", "alive": True},
            "Dead": {"type": "Shadowsocks", "alive": False},
            "D": {"type": "Trojan", "alive": True},
        }
        got = controller.build_candidates(group, proxies, allow_direct=False)
        self.assertEqual(got, ["C", "D", "A"])

    def test_allow_direct(self) -> None:
        group = {"now": "A", "all": ["DIRECT", "A", "B"]}
        proxies = {
            "DIRECT": {"type": "Direct"},
            "A": {"type": "Shadowsocks", "alive": True},
            "B": {"type": "Shadowsocks", "alive": True},
        }
        got = controller.build_candidates(group, proxies, allow_direct=True)
        self.assertEqual(got, ["B", "DIRECT"])

    def test_measured_node_survives_stale_alive_flag(self) -> None:
        """mihomo's cached alive flag can lag behind reality.

        A node with a measured success rate must stay in the candidate list
        even when the cache says dead; the live health probe still gates it.
        """
        group = {"now": "NOW", "all": ["NOW", "GOOD", "DEAD"]}
        proxies = {
            "NOW": {"type": "Shadowsocks", "alive": True},
            "GOOD": {"type": "Shadowsocks", "alive": False},
            "DEAD": {"type": "Shadowsocks", "alive": False},
        }
        without = controller.build_candidates(group, proxies, False)
        self.assertNotIn("GOOD", without)

        with_scores = controller.build_candidates(
            group, proxies, False, keep_nodes={"GOOD"}
        )
        self.assertIn("GOOD", with_scores)
        self.assertNotIn("DEAD", with_scores)

    def test_rotate(self) -> None:
        self.assertEqual(controller.rotate(["a", "b", "c"], 0), ["a", "b", "c"])
        self.assertEqual(controller.rotate(["a", "b", "c"], 1), ["b", "c", "a"])
        self.assertEqual(controller.rotate(["a", "b", "c"], 4), ["b", "c", "a"])
        self.assertEqual(controller.rotate([], 2), [])


class PolicyOwnershipTests(unittest.TestCase):
    """P2 and P3 must not both act on the same class, and not leave gaps."""

    def test_p3_owned_classes_are_not_p2_switch_classes(self) -> None:
        overlap = controller.P3_OWNED_CLASSES & controller.SWITCH_CLASSES
        self.assertEqual(overlap, set(), f"both layers act on {overlap}")

    def test_p3_owned_classes_are_not_p2_persistent_classes(self) -> None:
        overlap = controller.P3_OWNED_CLASSES & controller.PERSISTENT_CLASSES
        self.assertEqual(overlap, set(), f"both layers act on {overlap}")

    def test_p3_covers_every_p3_owned_class(self) -> None:
        import session_failover

        missing = controller.P3_OWNED_CLASSES - session_failover.FAILOVER_CLASSES
        self.assertEqual(missing, set(), f"P3 does not handle {missing}")

    def test_every_class_has_exactly_one_owner(self) -> None:
        import session_failover

        p2 = controller.SWITCH_CLASSES | controller.PERSISTENT_CLASSES
        p3 = session_failover.FAILOVER_CLASSES
        both = p2 & p3
        self.assertEqual(both, set(), f"both layers act on {both}")

    def test_p2_defers_p3_owned_class(self) -> None:
        decision = controller.decide(
            {"error_class": "AUTH_IP_REPUTATION"},
            {},
            1000.0,
            120.0,
            300.0,
            3,
            2,
        )
        self.assertEqual(decision.kind, "NOOP")
        self.assertIn("session_failover", decision.reason)

    def test_p2_still_switches_net_classes(self) -> None:
        decision = controller.decide(
            {"error_class": "NET_STREAM"},
            {},
            1000.0,
            120.0,
            300.0,
            3,
            2,
        )
        self.assertEqual(decision.kind, "SWITCH")


class DryRunBudgetTests(unittest.TestCase):
    """Dry-run must preview the same budget behaviour as --apply."""

    class FakeController:
        def __init__(self, apply: bool) -> None:
            self.args = type("A", (), {"apply": apply})()
            self.state = {"switch_times": [], "class_last_action": {}, "last_switch_ts": 0.0}
            self._dry_state = None

        budget_state = controller.Controller.budget_state
        record_attempt = controller.Controller.record_attempt

    def test_dry_run_consumes_shadow_budget_only(self) -> None:
        dry = self.FakeController(apply=False)
        dry.record_attempt("NET_DNS", 1000.0, success=False)
        # The real state is untouched...
        self.assertEqual(dry.state["switch_times"], [])
        # ...but the shadow carries the attempt.
        self.assertEqual(len(dry.budget_state()["switch_times"]), 1)

    def test_apply_uses_real_state(self) -> None:
        live = self.FakeController(apply=True)
        live.record_attempt("NET_DNS", 1000.0, success=False)
        self.assertEqual(len(live.state["switch_times"]), 1)
        self.assertEqual(len(live.budget_state()["switch_times"]), 1)


class NodeScoreTests(unittest.TestCase):
    """Measured success rate must outrank latency when picking a node."""

    class FakeClient:
        def __init__(self, delays: dict[str, int]) -> None:
            self.delays = delays

        def get_delay(self, name: str, url: str, timeout_ms: int) -> int:
            return self.delays[name]

    def test_scores_outrank_latency(self) -> None:
        # "fast" has the lowest latency but a poor success rate;
        # "good" is slower but measured far more reliable.
        client = self.FakeClient({"fast": 120, "good": 900})
        chosen, health = controller.probe_candidates(
            client,
            ["fast", "good"],
            "https://example.invalid",
            5000,
            5000,
            4,
            scores={"fast": 0.33, "good": 0.83},
        )
        self.assertEqual(chosen, "good")
        self.assertEqual(health["fast"], 120)

    def test_without_scores_latency_decides(self) -> None:
        client = self.FakeClient({"fast": 120, "slow": 900})
        chosen, _ = controller.probe_candidates(
            client,
            ["slow", "fast"],
            "https://example.invalid",
            5000,
            5000,
            4,
        )
        self.assertEqual(chosen, "fast")

    def test_dead_node_never_chosen(self) -> None:
        client = self.FakeClient({"dead": 9000, "good": 800})
        chosen, _ = controller.probe_candidates(
            client,
            ["dead", "good"],
            "https://example.invalid",
            5000,
            5000,
            4,
            scores={"dead": 0.99, "good": 0.5},
        )
        self.assertEqual(chosen, "good")

    def test_load_node_scores(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scores.json"
            path.write_text(
                json.dumps({"results": [{"node": "A", "rate": 0.75}, {"node": "B", "rate": 0.5}]}),
                encoding="utf-8",
            )
            scores = controller.load_node_scores(path)
            self.assertEqual(scores, {"A": 0.75, "B": 0.5})


class StateTests(unittest.TestCase):
    def test_state_roundtrip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = {"offsets": {"x": 1}, "switch_times": [1.0], "class_last_action": {}}
            controller.save_state(path, state)
            self.assertEqual(controller.load_state(path)["offsets"], {"x": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
