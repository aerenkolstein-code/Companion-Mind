import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "chatgpt_recovery_verify.py"
spec = importlib.util.spec_from_file_location("chatgpt_recovery_verify", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BrowserExportVerifierTest(unittest.TestCase):
    def test_preserves_json_stringify_number_lexemes(self) -> None:
        canonical = (
            '{"active_path_size":1,"mapping_size":1,'
            '"ordered_nodes":[{"metadata":{"threshold":0.000001,"tiny":1e-7}}],'
            '"schema_version":"cm-c2-recovery/v0.1"}'
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        file_text = (
            '{\n'
            '  "schema_version": "cm-c2-recovery/v0.1",\n'
            '  "mapping_size": 1,\n'
            '  "active_path_size": 1,\n'
            '  "ordered_nodes": [{"metadata": {"tiny": 1e-7, "threshold": 0.000001}}],\n'
            f'  "sha256": "{digest}"\n'
            '}\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_text(file_text, encoding="utf-8")
            receipt = module.verify_file(path)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["sha256"], digest)

    def test_detects_body_mutation(self) -> None:
        canonical = '{"active_path_size":1,"mapping_size":1,"ordered_nodes":[{"role":"user"}],"schema_version":"cm-c2-recovery/v0.1"}'
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        artifact = {
            "schema_version": "cm-c2-recovery/v0.1",
            "mapping_size": 1,
            "active_path_size": 1,
            "ordered_nodes": [{"role": "assistant"}],
            "sha256": digest,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(module.VerificationError, "checksum mismatch"):
                module.verify_file(path)

    def test_browser_string_encoding_escapes_quote_and_backslash(self) -> None:
        self.assertEqual(module._js_string('a"b\\c'), '"a\\"b\\\\c"')

    def test_browser_string_encoding_handles_lone_surrogate(self) -> None:
        self.assertEqual(module._js_string("\ud800"), '"\\ud800"')

    def test_object_key_sort_uses_utf16_order(self) -> None:
        value = {"\U00010000": module.RawNumber("1"), "\ue000": module.RawNumber("2")}
        rendered = module._canonical_browser_json(value)
        # JS default sort compares UTF-16 code units: D800 DC00 comes before E000.
        self.assertTrue(rendered.startswith('{"\U00010000":1,"\ue000":2'))


if __name__ == "__main__":
    unittest.main()
