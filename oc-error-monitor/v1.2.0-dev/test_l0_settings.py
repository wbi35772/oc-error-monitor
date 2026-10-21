#!/usr/bin/env python3
"""Regression tests for L0: durable settings and pool discovery.

The defect these lock down was found on the live box: SETTINGS lived in
memory only, so a reboot dropped force_route back to auto with no trace. The
429s that direct mode had been avoiding came back and nothing in the logs
said why.

The other half of L0 is the pool: "direct vs proxy" used to be a two-value
switch, but the subscriber keeps adding cfnew Workers, and each deployment is
a different uuid / path / domain. So the candidate set is read from the mihomo
group at runtime, and DIRECT becomes one member among many instead of a mode.

Run: python test_l0_settings.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retry_proxy


GROUP = "\U0001F3AF OpenCode\u4e13\u7528"


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class SettingsRoundTrip(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="ocm-l0-")
        self._path = os.path.join(self._dir, "settings.json")
        self._orig_path = retry_proxy.SETTINGS_PATH
        self._orig_settings = copy.deepcopy(retry_proxy.SETTINGS)
        self._orig_route = copy.deepcopy(retry_proxy.ROUTE)
        self._orig_raw = copy.deepcopy(retry_proxy._settings_raw)
        retry_proxy.SETTINGS_PATH = self._path
        retry_proxy._settings_raw = {}

    def tearDown(self):
        retry_proxy.SETTINGS_PATH = self._orig_path
        retry_proxy.SETTINGS.clear()
        retry_proxy.SETTINGS.update(self._orig_settings)
        retry_proxy.ROUTE.clear()
        retry_proxy.ROUTE.update(self._orig_route)
        retry_proxy._settings_raw = self._orig_raw

    def _reset_memory(self):
        retry_proxy.SETTINGS["force_route"] = None
        retry_proxy.SETTINGS["attempts"] = 4
        retry_proxy.ROUTE["select"]["pin"] = None
        retry_proxy.ROUTE["select"]["strategy"] = "least_fail"

    def test_route_survives_a_reload(self):
        retry_proxy.SETTINGS["force_route"] = "direct"
        retry_proxy.SETTINGS["attempts"] = 7
        ok, err = retry_proxy.save_settings("test")
        self.assertTrue(ok, err)

        self._reset_memory()
        retry_proxy._load_settings()

        self.assertEqual(retry_proxy.SETTINGS["force_route"], "direct")
        self.assertEqual(retry_proxy.SETTINGS["attempts"], 7)

    def test_unknown_keys_survive_at_every_level(self):
        retry_proxy.save_settings("seed")
        raw = _read_json(self._path)
        raw["future_layer"] = {"seed": 7}
        raw["route"]["unknown_route_knob"] = 42
        raw["route"].setdefault("select", {})["unknown_select_knob"] = "x"
        _write_json(self._path, raw)

        retry_proxy._load_settings()
        self.assertEqual(retry_proxy.ROUTE["select"]["unknown_select_knob"], "x")
        retry_proxy.save_settings("again")

        back = _read_json(self._path)
        self.assertEqual(back["future_layer"], {"seed": 7})
        self.assertEqual(back["route"]["unknown_route_knob"], 42)
        self.assertEqual(back["route"]["select"]["unknown_select_knob"], "x")

    def test_paused_is_not_persisted(self):
        retry_proxy.SETTINGS["paused"] = True
        retry_proxy.save_settings("test")
        raw = _read_json(self._path)
        self.assertNotIn("paused", raw["settings"])

        retry_proxy.SETTINGS["paused"] = False
        retry_proxy._load_settings()
        self.assertFalse(retry_proxy.SETTINGS["paused"])

    def test_hand_edited_values_are_clamped(self):
        raw = {
            "schema": 1,
            "settings": {
                "attempts": 999,
                "timeout": 1,
                "force_route": "sideways",
                "backoff": [1, 2, 3, 4, 5, 6, 7, 8],
            },
        }
        _write_json(self._path, raw)
        retry_proxy._load_settings()

        self.assertEqual(retry_proxy.SETTINGS["attempts"], 10)
        self.assertEqual(retry_proxy.SETTINGS["timeout"], 5.0)
        self.assertIsNone(retry_proxy.SETTINGS["force_route"])
        self.assertEqual(len(retry_proxy.SETTINGS["backoff"]), 6)

    def test_no_temp_file_is_left_behind(self):
        retry_proxy.save_settings("test")
        self.assertTrue(os.path.exists(self._path))
        self.assertFalse(os.path.exists(self._path + ".tmp"))

    def test_missing_file_keeps_defaults(self):
        retry_proxy._load_settings()
        self.assertIsNone(retry_proxy.SETTINGS["force_route"])
        self.assertEqual(retry_proxy.ROUTE["select"]["strategy"], "least_fail")

    def test_corrupt_file_keeps_defaults(self):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write("{not json")
        retry_proxy._load_settings()
        self.assertIsNone(retry_proxy.SETTINGS["force_route"])

    def test_only_differences_are_stored(self):
        retry_proxy.SETTINGS["force_route"] = "proxy"
        retry_proxy.save_settings("test")
        raw = _read_json(self._path)

        self.assertEqual(raw["route"], {})  # nothing was changed away from default
        retry_proxy.ROUTE["select"]["cooldown_s"] = 7
        retry_proxy.save_settings("test")
        raw = _read_json(self._path)
        self.assertEqual(raw["route"], {"select": {"cooldown_s": 7}})

    def test_a_changed_default_reaches_an_existing_file(self):
        retry_proxy.SETTINGS["force_route"] = "proxy"
        retry_proxy.save_settings("test")
        self.assertNotIn("rotate_on", _read_json(self._path)["route"])

        old = retry_proxy.DEFAULT_ROUTE["rotate_on"]
        retry_proxy.DEFAULT_ROUTE["rotate_on"] = ["NET_STREAM"]
        try:
            retry_proxy._load_settings()
            self.assertEqual(retry_proxy.ROUTE["rotate_on"], ["NET_STREAM"])
        finally:
            retry_proxy.DEFAULT_ROUTE["rotate_on"] = old


class PoolDiscovery(unittest.TestCase):
    def setUp(self):
        self._orig = (
            retry_proxy._mihomo,
            retry_proxy._clash_secret,
            retry_proxy.UPSTREAM,
            copy.deepcopy(retry_proxy.ROUTE),
        )
        retry_proxy._pool_cache["at"] = 0.0
        retry_proxy._pool_cache["data"] = None
        retry_proxy._clash_secret = lambda: ""

    def tearDown(self):
        (retry_proxy._mihomo,
         retry_proxy._clash_secret,
         retry_proxy.UPSTREAM,
         route) = self._orig
        retry_proxy.ROUTE.clear()
        retry_proxy.ROUTE.update(route)
        retry_proxy._pool_cache["at"] = 0.0
        retry_proxy._pool_cache["data"] = None

    def _stub(self, proxies, rules=None):
        table = {
            "/proxies": {"proxies": proxies},
            "/connections": {"connections": []},
            "/configs": {"mode": "rule"},
            "/rules": {"rules": rules or []},
        }
        retry_proxy._mihomo = lambda path, secret, timeout=1.8: table[path]

    def test_discovers_group_and_adds_direct_once(self):
        self._stub({
            GROUP: {"type": "Selector", "all": ["node-a", "DIRECT"], "now": "node-a"},
            "node-a": {"type": "Vless", "history": [{"delay": 120}]},
            "DIRECT": {"type": "Direct"},
        })
        pool = retry_proxy.discover_pool(force=True)

        self.assertTrue(pool["ok"], pool)
        self.assertEqual(pool["group"], GROUP)
        names = [m["name"] for m in pool["members"]]
        self.assertEqual(names.count("DIRECT"), 1)
        self.assertEqual(pool["count"], 2)
        self.assertFalse(
            [m for m in pool["members"] if m["name"] == "DIRECT"][0]["probeable"]
        )

    def test_config_group_name_wins_over_discovery(self):
        retry_proxy.ROUTE["pool"]["mihomo_group"] = "manual"
        self._stub({
            "manual": {"type": "Selector", "all": ["node-b"], "now": "node-b"},
            "node-b": {"type": "Vless", "history": []},
            GROUP: {"type": "Selector", "all": ["node-a"], "now": "node-a"},
            "node-a": {"type": "Vless", "history": []},
        })
        pool = retry_proxy.discover_pool(force=True)

        self.assertEqual(pool["group"], "manual")
        self.assertEqual(pool["basis"], "configured")
        self.assertEqual([m["name"] for m in pool["members"]], ["node-b", "DIRECT"])

    def test_extra_members_do_not_duplicate(self):
        retry_proxy.ROUTE["pool"]["extra_members"] = ["DIRECT", "ghost"]
        self._stub({
            GROUP: {"type": "Selector", "all": ["node-a", "DIRECT"], "now": "node-a"},
            "node-a": {"type": "Vless", "history": []},
            "DIRECT": {"type": "Direct"},
        })
        pool = retry_proxy.discover_pool(force=True)
        names = [m["name"] for m in pool["members"]]

        self.assertEqual(names.count("DIRECT"), 1)
        self.assertNotIn("ghost", names)  # nothing to describe it with

    def test_controller_down_is_reported_not_raised(self):
        def boom(path, secret, timeout=1.8):
            raise OSError("connection refused")

        retry_proxy._mihomo = boom
        pool = retry_proxy.discover_pool(force=True)

        self.assertFalse(pool["ok"])
        self.assertIn("connection refused", pool["reason"])


class PolicyVocabularyAgrees(unittest.TestCase):
    """The pool must not invent its own idea of which failures justify moving
    an exit IP: controller.SWITCH_CLASSES is the backtested set."""

    def test_rotate_on_matches_switch_classes(self):
        import controller

        self.assertEqual(
            set(retry_proxy.DEFAULT_ROUTE["rotate_on"]),
            set(controller.SWITCH_CLASSES),
        )

    def test_no_switch_reason_is_never_rotated_on(self):
        import controller

        overlap = set(retry_proxy.DEFAULT_ROUTE["rotate_on"]) & set(
            controller.NO_SWITCH_REASONS
        )
        self.assertEqual(overlap, set(), overlap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
