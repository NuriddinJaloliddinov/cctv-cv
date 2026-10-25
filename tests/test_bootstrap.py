"""Setup safety tests; never access the network or change real model files."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_download_verifies_hash_and_promotes_only_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.onnx"
            payload = b"a synthetic model fixture"
            digest = hashlib.sha256(payload).hexdigest()
            with patch("bootstrap.urllib.request.urlopen", return_value=io.BytesIO(payload)):
                bootstrap.ensure_download(path, "https://example.invalid/model", digest)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(list(Path(directory).glob("*.download")), [])

    def test_corrupt_download_leaves_no_model_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.onnx"
            with patch("bootstrap.urllib.request.urlopen", return_value=io.BytesIO(b"bad")):
                with self.assertRaisesRegex(bootstrap.SetupError, "Hash mismatch"):
                    bootstrap.ensure_download(path, "https://example.invalid/model", "0" * 64)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_file_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.onnx"
            target.write_bytes(b"user model")
            staging = Path(directory) / "staging"
            staging.write_bytes(b"official model")
            with self.assertRaises(bootstrap.SetupError):
                bootstrap.promote_new_file(staging, target)
            self.assertEqual(target.read_bytes(), b"user model")
            with patch("bootstrap.urllib.request.urlopen") as download:
                with self.assertRaisesRegex(bootstrap.SetupError, "not overwritten"):
                    bootstrap.ensure_download(target, "https://example.invalid", "0" * 64)
                download.assert_not_called()

    def test_gpu_ort_is_not_overwritten_by_cpu_package(self):
        seen = []

        def fake_run(command, **kwargs):
            seen.append(Path(command[-1]).read_text(encoding="utf-8"))

        with patch("bootstrap.venv_eval", return_value="yes"), \
             patch("bootstrap.run", side_effect=fake_run):
            bootstrap.install_requirements()
        self.assertEqual(len(seen), 1)
        self.assertNotIn("\nonnxruntime>=", seen[0])
        self.assertIn("\naiortc>=", seen[0])

    def test_export_only_publishes_validated_new_yolo(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "data/yolov8n.onnx"

            def fake_run(command, **kwargs):
                if "-c" in command:
                    (Path(kwargs["cwd"]) / "yolov8n.onnx").write_bytes(b"validated model")

            with patch.object(bootstrap, "ROOT", base), \
                 patch("bootstrap.venv.create"), \
                 patch("bootstrap.run", side_effect=fake_run), \
                 patch("bootstrap.verify_yolo_in_venv") as verify:
                bootstrap.ensure_yolo(target)
            self.assertEqual(target.read_bytes(), b"validated model")
            verify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
