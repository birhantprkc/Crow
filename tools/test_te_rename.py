"""te_rename.py on tiny synthetic safetensors: the rule, the refusals, the resume.

Every case builds its own shards in a temp dir; nothing here reads the 17.5 GB
upstream encoder. The real-data check (the four upstream headers read by HTTP
Range against the owner's derived copy) is recorded in te_rename's docstring.
"""

import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import te_rename as T  # noqa: E402


def write_st(path, tensors, metadata=None, pad=0):
    """tensors: list of (name, dtype, shape, payload bytes). Returns the header dict."""
    header = {}
    if metadata is not None:
        header["__metadata__"] = metadata
    blob = b""
    for name, dtype, shape, data in tensors:
        header[name] = {"dtype": dtype, "shape": shape,
                        "data_offsets": [len(blob), len(blob) + len(data)]}
        blob += data
    raw = json.dumps(header, separators=(",", ":")).encode() + b" " * pad
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(raw)) + raw + blob)
    return header


def slurp(path):
    with open(path, "rb") as f:
        return f.read()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_st(path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
        payload = f.read()
    return n, header, payload


SHARD1 = [("model.language_model.embed_tokens.weight", "BF16", [4, 2], bytes(range(16))),
          ("model.visual.blocks.0.attn.qkv.weight", "BF16", [2, 2], b"\x10" * 8),
          ("model.language_model.layers.0.input_layernorm.weight", "F32", [3], b"\x07" * 12)]
SHARD2 = [("lm_head.weight", "BF16", [4, 2], b"\xab" * 16),
          ("model.language_model.norm.weight", "BF16", [2], b"\x01\x02\x03\x04")]
S1 = "model-00001-of-00002.safetensors"
S2 = "model-00002-of-00002.safetensors"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "text_encoder")
        self.dst = os.path.join(self.tmp.name, "text_encoder_sdcli")
        os.makedirs(self.src)
        write_st(os.path.join(self.src, S1), SHARD1, metadata={"format": "pt"})
        write_st(os.path.join(self.src, S2), SHARD2, metadata={"format": "pt"}, pad=5)
        self.write_index({n: S1 for n, *_ in SHARD1} | {n: S2 for n, *_ in SHARD2})

    def tearDown(self):
        self.tmp.cleanup()

    def write_index(self, weight_map):
        with open(os.path.join(self.src, T.INDEX), "w", encoding="utf-8") as f:
            json.dump({"metadata": {"total_size": 56}, "weight_map": weight_map}, f, indent=2)

    def run_quiet(self):
        return T.convert_dir(self.src, self.dst, log=lambda *_: None)


class RuleTest(unittest.TestCase):
    def test_three_prefixes(self):
        self.assertEqual(T.rename_key("model.language_model.layers.3.mlp.up_proj.weight"),
                         "text_encoders.llm.model.layers.3.mlp.up_proj.weight")
        self.assertEqual(T.rename_key("model.visual.merger.linear_fc1.bias"),
                         "text_encoders.llm.model.visual.merger.linear_fc1.bias")
        self.assertEqual(T.rename_key("lm_head.weight"), "text_encoders.llm.lm_head.weight")

    def test_unknown_prefix_is_refused_by_name(self):
        for bad in ("model.embed_tokens.weight", "visual.blocks.0.norm1.weight",
                    "text_encoders.llm.model.norm.weight", "model.language_modelX.w"):
            with self.assertRaises(T.RenameError) as cm:
                T.rename_key(bad)
            self.assertIn(repr(bad), str(cm.exception))


class ConvertTest(Fixture):
    def test_names_renamed_offsets_and_payload_unchanged(self):
        self.run_quiet()
        for shard, spec in ((S1, SHARD1), (S2, SHARD2)):
            _, src_h, src_p = read_st(os.path.join(self.src, shard))
            n, h, p = read_st(os.path.join(self.dst, shard))
            self.assertNotIn("__metadata__", h)
            self.assertEqual(list(h), [T.rename_key(name) for name, *_ in spec])
            for name, *_ in spec:
                for field in ("dtype", "shape", "data_offsets"):
                    self.assertEqual(h[T.rename_key(name)][field], src_h[name][field])
            self.assertEqual(p, src_p)
            raw = slurp(os.path.join(self.dst, shard))[8:8 + n]
            self.assertEqual(raw, json.dumps(h, separators=(",", ":")).encode())
        idx = load_json(os.path.join(self.dst, T.INDEX))
        self.assertEqual(idx["metadata"], {"total_size": 56})
        self.assertEqual(idx["weight_map"]["text_encoders.llm.lm_head.weight"], S2)
        self.assertEqual(len(idx["weight_map"]), 5)
        self.assertFalse([f for f in os.listdir(self.dst) if f.endswith(".part")])

    def test_padded_source_header_is_read(self):
        n, h, _ = read_st(os.path.join(self.src, S2))
        raw = slurp(os.path.join(self.src, S2))[8:8 + n]
        self.assertTrue(raw.endswith(b"     "))
        self.run_quiet()
        T.check_output(os.path.join(self.src, S2), os.path.join(self.dst, S2))

    def test_unknown_prefix_in_a_shard_writes_nothing(self):
        bad = [("model.embed_tokens.weight", "BF16", [2], b"\0" * 4)]
        write_st(os.path.join(self.src, S2), bad)
        self.write_index({n: S1 for n, *_ in SHARD1} | {"model.embed_tokens.weight": S2})
        with self.assertRaises(T.RenameError) as cm:
            self.run_quiet()
        self.assertIn("model.embed_tokens.weight", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.dst, T.INDEX)))

    def test_index_naming_a_tensor_its_shard_lacks_is_refused(self):
        self.write_index({n: S1 for n, *_ in SHARD1} | {"lm_head.weight": S1,
                                                         "model.language_model.norm.weight": S2})
        with self.assertRaises(T.RenameError) as cm:
            self.run_quiet()
        self.assertIn("lm_head.weight", str(cm.exception))

    def test_corrupt_source_header_is_refused(self):
        with open(os.path.join(self.src, S1), "r+b") as f:
            f.write(struct.pack("<Q", 1 << 40))
        with self.assertRaises(T.RenameError):
            self.run_quiet()


class ResumeTest(Fixture):
    def test_rerun_skips_valid_outputs(self):
        self.run_quiet()
        self.assertEqual(dict(self.run_quiet()),
                         {S1: "skipped", S2: "skipped", T.INDEX: "written"})

    def test_damaged_output_and_stale_part_are_redone(self):
        self.run_quiet()
        out = os.path.join(self.dst, S2)
        with open(out, "r+b") as f:
            f.truncate(os.path.getsize(out) - 3)
        os.remove(os.path.join(self.dst, S1))
        with open(os.path.join(self.dst, S1 + ".part"), "wb") as f:
            f.write(b"half a shard from a killed run")
        self.assertEqual(dict(self.run_quiet()),
                         {S1: "written", S2: "written", T.INDEX: "written"})
        for shard in (S1, S2):
            self.assertEqual(read_st(os.path.join(self.dst, shard))[2],
                             read_st(os.path.join(self.src, shard))[2])
        self.assertFalse([f for f in os.listdir(self.dst) if f.endswith(".part")])


class VerifyTest(Fixture):
    def test_payload_difference_is_caught(self):
        self.run_quiet()
        out = os.path.join(self.dst, S1)
        with open(out, "r+b") as f:
            f.seek(-1, os.SEEK_END)
            f.write(b"\xff")
        sn = read_st(os.path.join(self.src, S1))[0]
        dn = read_st(out)[0]
        T.check_output(os.path.join(self.src, S1), out)  # structure still matches
        with self.assertRaises(T.RenameError):
            T.compare_payload(os.path.join(self.src, S1), sn, out, dn)

    def test_wrong_shape_is_caught(self):
        self.run_quiet()
        out = os.path.join(self.dst, S2)
        _, h, p = read_st(out)
        h["text_encoders.llm.model.norm.weight"]["shape"] = [1, 2]
        raw = json.dumps(h, separators=(",", ":")).encode()
        with open(out, "wb") as f:
            f.write(struct.pack("<Q", len(raw)) + raw + p)
        with self.assertRaises(T.RenameError) as cm:
            T.check_output(os.path.join(self.src, S2), out)
        self.assertIn("shape", str(cm.exception))


class CliTest(Fixture):
    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "te_rename.py"), *args],
                              capture_output=True, text=True)

    def test_exit_codes(self):
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual(self.cli(self.src, self.src).returncode, 2)
        self.assertEqual(self.cli(self.src, self.dst).returncode, 0)
        self.assertEqual(self.cli(self.dst + "-missing", self.dst).returncode, 1)


if __name__ == "__main__":
    unittest.main()
