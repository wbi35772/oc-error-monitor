#!/usr/bin/env python3
"""Regression tests for pointing the client at this instance.

The problem this solves: the client config carries one api override per
model, and opencode honours no other form. Measured on the live box (E-013):
with `provider.opencode.api` and `options.baseURL` both set and one model's
per-model override removed, the request went straight to opencode.ai and
never reached the proxy. So the port cannot be a constant in the config.

The design: the running instance reports its own port into one state file,
and every api override in the client config is rewritten from that value.

Run: python test_client_pointing.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retry_proxy


SAMPLE = """{
    "provider": {
        "opencode": {
            "models": {
                "alpha": { "provider": { "api": "http://127.0.0.1:8788/v1" } },
                "beta":  { "provider": { "api": "http://127.0.0.1:8788/v1" } }
            }
        }
    }
}
"""


class ClientPointingBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="ocm-client-")
        self._cfg = os.path.join(self._dir, "opencode.json")
        self._target = os.path.join(self._dir, "target.json")
        self._orig = (
            retry_proxy.CLIENT_CONFIG_FILE,
            retry_proxy.CLIENT_TARGET_FILE,
            retry_proxy.PORT,
        )
        with open(self._cfg, "w", encoding="utf-8") as f:
            f.write(SAMPLE)
        retry_proxy.CLIENT_CONFIG_FILE = self._cfg
        retry_proxy.CLIENT_TARGET_FILE = self._target

    def tearDown(self):
        (retry_proxy.CLIENT_CONFIG_FILE,
         retry_proxy.CLIENT_TARGET_FILE,
         retry_proxy.PORT) = self._orig

    def _read(self):
        with open(self._cfg, encoding="utf-8") as f:
            return f.read()


class Rewrite(ClientPointingBase):
    def test_rewrites_every_override(self):
        ok, count, err = retry_proxy._rewrite_client_config(
            self._cfg, "http://127.0.0.1:8899/v1"
        )
        self.assertTrue(ok, err)
        self.assertEqual(count, 2)
        text = self._read()
        self.assertNotIn("8788", text)
        self.assertNotIn("/v1/v1", text)
        data = json.loads(text)
        for model in data["provider"]["opencode"]["models"].values():
            self.assertEqual(
                model["provider"]["api"], "http://127.0.0.1:8899/v1"
            )

    def test_leaves_other_services_alone(self):
        with open(self._cfg, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "provider": {
                    "opencode": {
                        "models": {
                            "alpha": {"provider": {"api": "http://127.0.0.1:8788/v1"}},
                            "other": {"provider": {"api": "https://api.example.com/v1"}},
                        }
                    }
                }
            }, indent=2))
        ok, count, err = retry_proxy._rewrite_client_config(
            self._cfg, "http://127.0.0.1:8899/v1"
        )
        self.assertTrue(ok, err)
        self.assertEqual(count, 1)
        text = self._read()
        self.assertIn("https://api.example.com/v1", text)
        self.assertIn("http://127.0.0.1:8899/v1", text)

    def test_no_overrides_is_not_an_error(self):
        with open(self._cfg, "w", encoding="utf-8") as f:
            f.write('{"provider": {}}')
        changed, count, err = retry_proxy._rewrite_client_config(
            self._cfg, "http://127.0.0.1:8899/v1"
        )
        # Nothing matched, so nothing was written - and that is not a failure.
        self.assertFalse(changed)
        self.assertEqual(count, 0)
        self.assertIsNone(err)

    def test_a_broken_file_is_not_written(self):
        broken = '{"provider": {'
        with open(self._cfg, "w", encoding="utf-8") as f:
            f.write(broken)
        changed, count, err = retry_proxy._rewrite_client_config(
            self._cfg, "http://127.0.0.1:8899/v1"
        )
        self.assertFalse(changed)
        self.assertEqual(count, 0)
        self.assertIsNone(err)
        self.assertEqual(self._read(), broken)

    def test_a_broken_file_with_an_override_is_refused(self):
        broken = '{"provider": {"a": {"api": "http://127.0.0.1:8788/v1"'
        with open(self._cfg, "w", encoding="utf-8") as f:
            f.write(broken)
        changed, count, err = retry_proxy._rewrite_client_config(
            self._cfg, "http://127.0.0.1:8899/v1"
        )
        self.assertFalse(changed)
        self.assertIsNotNone(err)
        self.assertEqual(self._read(), broken)

    def test_byte_order_mark_survives(self):
        with open(self._cfg, "wb") as f:
            f.write(b"\xef\xbb\xbf" + SAMPLE.encode("utf-8"))
        ok, count, err = retry_proxy._rewrite_client_config(
            self._cfg, "http://127.0.0.1:8899/v1"
        )
        self.assertTrue(ok, err)
        with open(self._cfg, "rb") as f:
            self.assertEqual(f.read(3), b"\xef\xbb\xbf")

    def test_rewriting_is_idempotent(self):
        retry_proxy._rewrite_client_config(self._cfg, "http://127.0.0.1:8899/v1")
        first = self._read()
        ok, count, err = retry_proxy._rewrite_client_config(
            self._cfg, "http://127.0.0.1:8899/v1"
        )
        self.assertTrue(ok, err)
        self.assertEqual(count, 2)
        self.assertEqual(self._read(), first)


class PointClient(ClientPointingBase):
    def test_pointing_writes_the_state_file(self):
        out = retry_proxy.point_client(port=8899, reason="test")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["base_url"], "http://127.0.0.1:8899/v1")
        self.assertEqual(out["overrides"], 2)

        with open(self._target, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["port"], 8899)
        self.assertEqual(state["base_url"], "http://127.0.0.1:8899/v1")
        self.assertEqual(state["version"], retry_proxy.VERSION)
        self.assertEqual(state["reason"], "test")

    def test_the_state_file_is_what_read_client_target_returns(self):
        retry_proxy.point_client(port=8899, reason="test")
        target = retry_proxy.read_client_target()
        self.assertEqual(target["port"], 8899)
        self.assertEqual(target["base_url"], "http://127.0.0.1:8899/v1")

    def test_no_state_file_reads_as_none(self):
        self.assertIsNone(retry_proxy.read_client_target())

    def test_a_corrupt_state_file_reads_as_none(self):
        with open(self._target, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(retry_proxy.read_client_target())

    def test_the_port_comes_from_the_instance_not_the_caller(self):
        retry_proxy.PORT = 8899
        out = retry_proxy.point_client(reason="test")
        self.assertEqual(out["port"], 8899)
        self.assertEqual(out["base_url"], "http://127.0.0.1:8899/v1")

    def test_pointing_twice_is_stable(self):
        retry_proxy.point_client(port=8899, reason="a")
        first = self._read()
        retry_proxy.point_client(port=8899, reason="b")
        self.assertEqual(self._read(), first)
        self.assertEqual(retry_proxy.read_client_target()["reason"], "b")

    def test_an_explicit_port_moves_the_client_elsewhere(self):
        # The instance can point the client at another instance without the
        # server moving: useful when a build does not have the action yet.
        out = retry_proxy.point_client(port=8788, reason="test")
        self.assertEqual(out["base_url"], "http://127.0.0.1:8788/v1")
        self.assertEqual(out["port"], 8788)
        self.assertEqual(
            retry_proxy.read_client_target()["base_url"],
            "http://127.0.0.1:8788/v1",
        )
        data = json.loads(self._read())
        for model in data["provider"]["opencode"]["models"].values():
            self.assertEqual(
                model["provider"]["api"], "http://127.0.0.1:8788/v1"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
