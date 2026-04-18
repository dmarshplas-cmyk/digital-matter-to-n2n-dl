"""
Tests for n2n_common: token sanitisation, device ID, and passthrough transform.
No mapping config — all channels forwarded with self-describing labels.
"""
import hashlib
import re
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../lambdas/ingest"))

from n2n_common import to_n2n_token, make_n2n_device_id, basic_auth_header, build_reading_list

SAMPLE_PAYLOAD = {
    "date": "2026-03-19T10:55:31.7295108Z",
    "device": {"sn": "3333333", "prod": 160, "rev": 1, "fw": "2.2"},
    "sqn": 5083, "reason": 5,
    "lat": 71.58206949968837, "lng": -103.60468110549655,
    "posAcc": 13.874583356269843, "alt": 222, "spd": 389, "head": 73,
    "analogues": [
        {"id": 1,  "val": 7343},
        {"id": 3,  "val": 8705},
        {"id": 4,  "val": 98},
        {"id": 5,  "val": 7268},
        {"id": 12, "val": 39},
        {"id": 13, "val": -28},
        {"id": 17, "val": 517},
        {"id": 18, "val": 1850},
        {"id": 19, "val": 18},
        {"id": 20, "val": 52},
    ],
    "inputs": 7,    # binary 0b111 → bits 0, 1, 2 set
    "outputs": 6,   # binary 0b110 → bits 1, 2 set
    "status": 1,
    "counters": [
        {"id": 0,   "val": 6142},
        {"id": 3,   "val": 799},
        {"id": 128, "val": 2949},
        {"id": 142, "val": 5336442},
    ],
}


class TestToN2NToken:
    def test_basic(self):                    assert to_n2n_token("temperature") == "temperature"
    def test_underscores(self):              assert to_n2n_token("mould_risk") == "mould-risk"
    def test_spaces(self):                   assert to_n2n_token("battery voltage") == "battery-voltage"
    def test_uppercase(self):                assert to_n2n_token("TEMP") == "temp"
    def test_strips_invalid(self):           assert to_n2n_token("abc@!") == "abc"
    def test_collapses_hyphens(self):        assert to_n2n_token("a--b") == "a-b"
    def test_trims_hyphens(self):            assert to_n2n_token("-foo-") == "foo"
    def test_short_uses_fallback(self):      assert len(to_n2n_token("ab", "val")) >= 3
    def test_empty_uses_fallback(self):      assert len(to_n2n_token("", "sensor")) >= 3
    def test_none_uses_fallback(self):       assert len(to_n2n_token(None, "val")) >= 3
    def test_numeric(self):                  assert to_n2n_token(160) == "160"


class TestMakeN2NDeviceId:
    def test_format(self):
        parts = make_n2n_device_id("nnnc-housing", "3333333").split("-", 1)
        assert len(parts[0]) == 5 and parts[1] == "3333333"

    def test_prefix_is_md5(self):
        prefix = hashlib.md5(b"nnnc-housing").hexdigest()[:5]
        assert make_n2n_device_id("nnnc-housing", "3333333").startswith(prefix)

    def test_serial_sanitised(self):
        assert "SN_12345" not in make_n2n_device_id("ent", "SN_12345")

    def test_deterministic(self):
        assert make_n2n_device_id("e", "d01") == make_n2n_device_id("e", "d01")


class TestBasicAuthHeader:
    def test_prefix(self):
        assert basic_auth_header("key").startswith("Basic ")

    def test_decodes(self):
        import base64
        decoded = base64.b64decode(basic_auth_header("mykey")[6:]).decode()
        assert decoded == "api:mykey"


class TestBuildReadingList:
    def setup_method(self):
        self.readings = build_reading_list(SAMPLE_PAYLOAD)
        self.by_label = {r["label"]: r for r in self.readings}

    # ── Position fields ──────────────────────────────────────────────────────
    def test_position_fields_present(self):
        for lbl in ["latitude", "longitude", "altitude", "speed", "heading", "pos-accuracy"]:
            assert lbl in self.by_label

    def test_latitude_value(self):
        assert abs(self.by_label["latitude"]["value"] - 71.582069) < 0.001

    def test_speed_divided_by_100(self):
        # spd=389 → 3.89 km/h
        assert abs(self.by_label["speed"]["value"] - 3.89) < 0.01

    # ── Analogue passthrough ─────────────────────────────────────────────────
    def test_all_analogues_present(self):
        for ch_id in [1, 3, 4, 5, 12, 13, 17, 18, 19, 20]:
            assert f"analogue-{ch_id}" in self.by_label

    def test_analogue_raw_value_unchanged(self):
        # No scale applied — value must be exactly what DM sent
        assert self.by_label["analogue-1"]["value"]  == 7343
        assert self.by_label["analogue-3"]["value"]  == 8705
        assert self.by_label["analogue-12"]["value"] == 39
        assert self.by_label["analogue-13"]["value"] == -28

    def test_analogue_type_label(self):
        assert self.by_label["analogue-1"]["type"] == "analog"

    # ── Counter passthrough ──────────────────────────────────────────────────
    def test_all_counters_present(self):
        for ct_id in [0, 3, 128, 142]:
            assert f"counter-{ct_id}" in self.by_label

    def test_counter_raw_value_unchanged(self):
        assert self.by_label["counter-142"]["value"] == 5336442
        assert self.by_label["counter-0"]["value"]   == 6142

    # ── Digital input bitmask decoding ───────────────────────────────────────
    def test_set_input_bits_present(self):
        # inputs=7 (0b111) → bits 0, 1, 2
        for bit in [0, 1, 2]:
            assert f"input-bit-{bit}" in self.by_label

    def test_unset_input_bits_absent(self):
        # bits 3+ are not set in inputs=7
        for bit in [3, 4, 24, 25, 29]:
            assert f"input-bit-{bit}" not in self.by_label

    def test_set_input_bit_value_is_1(self):
        # Only set bits are emitted, always with value=1
        assert self.by_label["input-bit-0"]["value"] == 1
        assert self.by_label["input-bit-1"]["value"] == 1
        assert self.by_label["input-bit-2"]["value"] == 1

    def test_output_bits_decoded(self):
        # outputs=6 (0b110) → only bits 1 and 2 are set → only those emitted
        assert "output-bit-1" in self.by_label
        assert "output-bit-2" in self.by_label
        assert "output-bit-0" not in self.by_label   # bit 0 not set in 0b110
        assert "output-bit-3" not in self.by_label   # bit 3 not set
        assert self.by_label["output-bit-1"]["value"] == 1
        assert self.by_label["output-bit-2"]["value"] == 1

    # ── General correctness ──────────────────────────────────────────────────
    def test_channel_ids_sequential(self):
        ids = [r["channelId"] for r in self.readings]
        assert ids == list(range(len(self.readings)))

    def test_all_labels_valid_n2n_tokens(self):
        pattern = re.compile(r'^[a-z0-9-]{3,}$')
        for r in self.readings:
            assert pattern.match(r["label"]), f"Bad label: {r['label']}"
            assert pattern.match(r["type"]),  f"Bad type:  {r['type']}"

    def test_no_mapping_config_needed(self):
        # build_reading_list takes only the payload — no mapping arg
        import inspect
        sig = inspect.signature(build_reading_list)
        assert list(sig.parameters.keys()) == ["dm_payload"]
