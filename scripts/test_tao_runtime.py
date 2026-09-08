"""Release preparation invariants; run python -m unittest discover -s scripts -p test_tao_runtime.py."""

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import tao_runtime as runtime


class RuntimeInputTests(unittest.TestCase):
    def test_refuses_modified_official_jar_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "bad.jar"
            source.write_bytes(b"not the official binary")
            output = root / "output"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "Expected official"):
                runtime.extract_base(source, output, "mpv", "macos-x64")
            self.assertEqual([], list(output.iterdir()))

    def test_refuses_archive_path_traversal_even_with_matching_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "bad.jar"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("../outside", "invalid")
            checksums = {"test": {"test": runtime.sha256(source)}}
            with patch.object(runtime, "BASE_SHA256", checksums):
                with self.assertRaisesRegex(ValueError, "Invalid"):
                    runtime.extract_base(source, root / "output", "test", "test")
            self.assertFalse((root / "outside").exists())

    def test_checks_actual_machine_type_not_just_file_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "native.dll"
            data = bytearray(256)
            data[:2] = b"MZ"
            struct.pack_into("<I", data, 0x3c, 128)
            data[128:132] = b"PE\0\0"
            struct.pack_into("<H", data, 132, 0xaa64)
            binary.write_bytes(data)
            runtime.verify_binary_arch(binary, "windows-arm64")
            with self.assertRaisesRegex(ValueError, "Wrong architecture"):
                runtime.verify_binary_arch(binary, "windows-x64")

    def test_ffmpeg_abi_changes_cannot_be_relabelled_as_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "jni.c").write_bytes(b"old JNI\n")
            digest = hashlib.sha256(b"jni.c\0old JNI\n\0").hexdigest()
            with (patch.object(runtime, "ROOT", root),
                  patch.object(runtime, "FFMPEG_COMPATIBILITY_FILES", ("jni.c",)),
                  patch.object(runtime, "FFMPEG_SUBMODULES", {}),
                  patch.object(runtime, "FFMPEG_COMPATIBILITY_SHA256", digest)):
                self.assertEqual(digest, runtime.ffmpeg_compatibility_source_hash())
                (root / "jni.c").write_bytes(b"old JNI\r\n")
                self.assertEqual(digest, runtime.ffmpeg_compatibility_source_hash())
                (root / "jni.c").write_bytes(b"changed JNI\n")
                with self.assertRaisesRegex(ValueError, "differ from upstream"):
                    runtime.ffmpeg_compatibility_source_hash()

    def test_manifest_requires_every_listed_native_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ffmpeg-natives.txt").write_text("libffmpegkitjni.so\n")
            with self.assertRaisesRegex(ValueError, "Missing native manifest entry"):
                runtime.verify_runtime_manifest(root, "ffmpeg", "linux-x64")


if __name__ == "__main__":
    unittest.main()
