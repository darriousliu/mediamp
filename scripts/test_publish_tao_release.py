"""Offline full-publication and immutable incremental-release regression checks."""

import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
import zipfile

from tao_runtime import BASE_SHA256, ffmpeg_compatibility_source_hash


spec = importlib.util.spec_from_file_location("publish_tao", Path(__file__).with_name("publish-tao-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

VERSION = "0.4.0-tao"
PLATFORMS = list(release.SUPPORTED_NATIVE)
EXISTING = [
    "mediamp-api", "mediamp-api-desktop", "mediamp-internal-utils", "mediamp-internal-utils-desktop",
    "mediamp-native-loader", "mediamp-native-loader-desktop", "mediamp-mpv", "mediamp-mpv-desktop",
    "mediamp-mpv-tao", "mediamp-mpv-runtime-macos-arm64", "mediamp-mpv-runtime-windows-x64",
]
CREDENTIALS = {"ORG_GRADLE_PROJECT_mavenCentralUsername": "fixture-user",
               "ORG_GRADLE_PROJECT_mavenCentralPassword": "fixture-password"}


def jar(entries=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, contents in (entries or {"marker": "fixture"}).items():
            archive.writestr(name, contents)
    return buffer.getvalue()


def elf(abi):
    bits, machine = {"armeabi-v7a": (1, 40), "arm64-v8a": (2, 183), "x86": (1, 3), "x86_64": (2, 62)}[abi]
    data = bytearray(64)
    data[:4], data[4], data[5], data[18:20] = b"\x7fELF", bits, 1, machine.to_bytes(2, "little")
    return data


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg_hash = ffmpeg_compatibility_source_hash()
        cls.mpv_hash = release.jni_source_hash()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.staging, self.output = self.root / "staging", self.root / "output"
        for artifact, kind in release.publication_specs().items():
            directory = self.directory(artifact)
            directory.mkdir(parents=True)
            prefix = artifact + "-" + VERSION
            self.file(artifact, ".pom").write_text(
                '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
                f"<groupId>{release.GROUP}</groupId><artifactId>{artifact}</artifactId>"
                f"<version>{VERSION}</version></project>")
            for suffix in release.required_suffixes(kind) - {".pom", ".module"}:
                self.file(artifact, suffix).write_bytes(jar())
            if kind == "catalog":
                self.file(artifact, ".toml").write_text(
                    f'[versions]\nmediamp = "{VERSION}"\n[libraries]\n'
                    f'api = {{ module = "{release.GROUP}:mediamp-api", version.ref = "mediamp" }}\n')
            if kind == "runtime":
                family = "mpv" if artifact.startswith("mediamp-mpv-") else "ffmpeg"
                platform = artifact.removeprefix(f"mediamp-{family}-runtime-")
                wrapper = {"mpv": {"windows": "mediampv.dll", "macos": "libmediampv.dylib", "linux": "libmediampv.so"},
                           "ffmpeg": {"windows": "ffmpegkitjni.dll", "macos": "libffmpegkitjni.dylib", "linux": "libffmpegkitjni.so"}}[family][platform.split("-")[0]]
                manifest = f"mpv-natives-{platform}.txt" if family == "mpv" else "ffmpeg-natives.txt"
                self.file(artifact, ".jar").write_bytes(jar({
                    "META-INF/mediamp-tao-native-build.txt": f"version={VERSION}\nbase-runtime=0.4.0\n"
                    f"base-sha256={BASE_SHA256[family][platform]}\njni-binary-sha256={release.digest(b'native fixture')}\n"
                    f"jni-source-sha256={self.mpv_hash}\ncompatibility-source-sha256={self.ffmpeg_hash}\n"
                    f"jni={'compiled-from-checkout' if family == 'mpv' else 'reused-upstream-unchanged'}\n",
                    manifest: wrapper + "\n", wrapper: b"native fixture"}))
            if kind == "android":
                contents = {"classes.jar": jar({"META-INF/licenses/org.openani.mediamp/mediamp-exoplayer/scaletempo2.txt": "license"})}
                wrapper = {"mediamp-mpv-android": "libmediampv.so", "mediamp-ffmpeg-android": "libffmpegkitjni.so",
                           "mediamp-exoplayer-android": "libmediamp_wsola.so"}.get(artifact)
                if wrapper:
                    for abi in release.INVENTORY["androidAbis"]:
                        contents[f"jni/{abi}/{wrapper}"] = elf(abi)
                        if artifact != "mediamp-exoplayer-android":
                            for library in ("libavcodec.so", "libavformat.so", "libavutil.so", "libmpv.so", "libc++_shared.so"):
                                contents[f"jni/{abi}/{library}"] = elf(abi)
                self.file(artifact, ".aar").write_bytes(jar(contents))
            if kind == "xcframework":
                contents, libraries = {}, []
                for identifier, variant in (("ios-arm64", None), ("ios-arm64-simulator", "simulator")):
                    entry = {"LibraryIdentifier": identifier, "LibraryPath": "MediampFFmpegKit.framework",
                             "SupportedPlatform": "ios", "SupportedArchitectures": ["arm64"]}
                    if variant:
                        entry["SupportedPlatformVariant"] = variant
                    libraries.append(entry)
                    contents[f"MediampFFmpegKit.xcframework/{identifier}/MediampFFmpegKit.framework/MediampFFmpegKit"] = b"binary"
                contents["MediampFFmpegKit.xcframework/Info.plist"] = plistlib.dumps({"AvailableLibraries": libraries})
                self.file(artifact, ".zip").write_bytes(jar(contents))
            if ".module" in release.required_suffixes(kind):
                variants = []
                if kind != "aggregate":
                    filename = prefix + release.primary_extension(kind)
                    variants.append({"name": "runtimeElements", "files": [{"name": filename, "url": filename,
                                     "sha256": release.digest((directory / filename).read_bytes())}]})
                for target in release.INVENTORY["multiplatform"].get(artifact, []):
                    target_artifact = artifact + "-" + target
                    variants.append({"name": target, "available-at": {"group": release.GROUP,
                        "module": target_artifact, "version": VERSION,
                        "url": f"../../{target_artifact}/{VERSION}/{target_artifact}-{VERSION}.module"}})
                self.file(artifact, ".module").write_text(json.dumps({"formatVersion": "1.1",
                    "component": {"group": release.GROUP, "module": artifact, "version": VERSION}, "variants": variants}))

    def directory(self, artifact):
        return self.staging / release.GROUP_PATH / artifact / VERSION

    def file(self, artifact, suffix):
        return self.directory(artifact) / (artifact + "-" + VERSION + suffix)

    def freeze(self, supplement=False):
        release.freeze(self.staging, self.output, VERSION, supplement_existing=supplement)
        return release.checked_manifest(self.output, VERSION, supplement_existing=supplement)

    def existing_server(self, artifacts=EXISTING):
        remote = {}
        for artifact in artifacts:
            for path in self.directory(artifact).iterdir():
                remote[path.relative_to(self.staging).as_posix()] = path.read_bytes()

        def request(url, **kwargs):
            relative = unquote(url.removeprefix(release.CENTRAL_REPOSITORY + "/"))
            if relative in remote:
                return remote[relative]
            names = [name[len(relative):] for name in remote if name.startswith(relative)]
            if relative.endswith("/") and names:
                return "".join(f'<a href="{name}">{name}</a>' for name in names).encode()
            raise HTTPError(url, 404, "missing", {}, io.BytesIO())
        return request, remote

    def supplemental_manifest(self):
        server, _ = self.existing_server()
        with patch.object(release, "request", side_effect=server):
            return self.freeze(True)

    def test_complete_sixty_five_coordinate_closure(self):
        manifest = self.freeze()
        self.assertEqual(len(manifest["coordinates"]), 65)
        for coordinate in ("mediamp-mpv-runtime", "mediamp-ffmpeg-runtime", "mediamp-exoplayer-android",
                           "mediamp-avkit-iosarm64", "mediamp-api-wasm-js", "mediamp-ffmpeg-runtime-ios-xcframework"):
            self.assertIn(coordinate, manifest["coordinates"])

    def test_platform_reduction_is_rejected(self):
        with self.assertRaisesRegex(release.ReleaseError, "every native platform"):
            release.coordinates(["macos-arm64", "windows-x64"])

    def test_missing_mobile_artifact_cannot_freeze(self):
        self.file("mediamp-api-iosarm64", ".klib").unlink()
        with self.assertRaisesRegex(release.ReleaseError, "Incomplete staging"):
            self.freeze()

    def test_runtime_missing_sources_cannot_freeze(self):
        self.file("mediamp-ffmpeg-runtime-macos-arm64", "-sources.jar").unlink()
        with self.assertRaisesRegex(release.ReleaseError, "Incomplete staging.*sources.jar"):
            self.freeze()

    def test_broken_available_at_cannot_freeze(self):
        path = self.file("mediamp-api", ".module")
        module = json.loads(path.read_text())
        module["variants"][1]["available-at"]["module"] = "missing-module"
        path.write_text(json.dumps(module))
        with self.assertRaisesRegex(release.ReleaseError, "Unpublished platform variant"):
            self.freeze()

    def test_upstream_pom_dependency_is_rejected(self):
        path = self.file("mediamp-mpv-tao", ".pom")
        path.write_text(path.read_text().replace("</project>", "<dependencies><dependency>"
                        "<groupId>org.openani.mediamp</groupId><artifactId>mediamp-api</artifactId>"
                        f"<version>{VERSION}</version></dependency></dependencies></project>"))
        with self.assertRaisesRegex(release.ReleaseError, "upstream namespace"):
            self.freeze()

    def test_catalog_cannot_reference_unpublished_compose_alias(self):
        path = self.file("catalog", ".toml")
        path.write_text(path.read_text().replace(":mediamp-api", ":mediamp-compose"))
        with self.assertRaisesRegex(release.ReleaseError, "Catalog alias"):
            release.validate_catalog(path, release.coordinates(), VERSION)

    def test_android_empty_backend_is_rejected(self):
        with zipfile.ZipFile(io.BytesIO(jar())) as archive:
            with self.assertRaisesRegex(release.ReleaseError, "Android native backend is missing"):
                release.validate_android(archive, "mediamp-mpv-android")

    def test_android_wrong_elf_architecture_is_rejected(self):
        with self.assertRaisesRegex(release.ReleaseError, "Wrong ELF architecture"):
            release.validate_elf(elf("x86_64"), "arm64-v8a", "fixture")

    def test_android_mpv_requires_libmpv_and_shared_cpp_runtime(self):
        path = self.file("mediamp-mpv-android", ".aar")
        with zipfile.ZipFile(path) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        for library in ("libmpv.so", "libc++_shared.so"):
            with self.subTest(library=library):
                incomplete = {name: data for name, data in contents.items() if not name.endswith("/" + library)}
                with zipfile.ZipFile(io.BytesIO(jar(incomplete))) as archive:
                    with self.assertRaisesRegex(release.ReleaseError, "Android codec dependency is missing"):
                        release.validate_android(archive, "mediamp-mpv-android")

    def test_new_native_runtime_requires_matching_source_and_binary_provenance(self):
        artifact = "mediamp-mpv-runtime-windows-arm64"
        with zipfile.ZipFile(self.file(artifact, ".jar")) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        name = "META-INF/mediamp-tao-native-build.txt"
        for field, value, message in (("base-sha256", BASE_SHA256["mpv"]["windows-arm64"], "upstream input"),
                                     ("jni-binary-sha256", release.digest(b"native fixture"), "binary checksum")):
            with self.subTest(field=field):
                altered = {**contents, name: contents[name].replace(f"{field}={value}".encode(), f"{field}=wrong".encode())}
                with zipfile.ZipFile(io.BytesIO(jar(altered))) as archive:
                    with self.assertRaisesRegex(release.ReleaseError, message):
                        release.validate_runtime(archive, artifact, VERSION)

    def test_existing_native_runtime_preserves_legacy_provenance(self):
        artifact = "mediamp-mpv-runtime-windows-x64"
        with zipfile.ZipFile(self.file(artifact, ".jar")) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        name = "META-INF/mediamp-tao-native-build.txt"
        contents[name] = (f"version={VERSION}\r\nbase-runtime=0.4.0\r\njni-source-sha256=original-published-checkout\r\n").encode()
        with zipfile.ZipFile(io.BytesIO(jar(contents))) as archive:
            release.validate_runtime(archive, artifact, VERSION, already_published=True)

    def test_xcframework_requires_device_and_simulator(self):
        content = jar({"MediampFFmpegKit.xcframework/Info.plist": plistlib.dumps({"AvailableLibraries": []})})
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            with self.assertRaisesRegex(release.ReleaseError, "device and simulator"):
                release.validate_xcframework(archive)

    def test_frozen_file_tampering_is_rejected(self):
        manifest = self.freeze()
        path = self.output / "payload" / next(iter(manifest["sha256"]))
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(release.ReleaseError, "has changed"):
            release.checked_manifest(self.output, VERSION)

    def test_supplement_keeps_original_central_bytes_instead_of_rebuilt_files(self):
        server, original = self.existing_server()
        path = self.file("mediamp-api", ".jar")
        path.write_bytes(jar({"changed": "new nondeterministic local build"}))
        with patch.object(release, "request", side_effect=server):
            manifest = self.freeze(True)
        self.assertEqual(len(manifest["existingCoordinates"]), 11)
        self.assertEqual(len(manifest["uploadCoordinates"]), 54)
        relative = path.relative_to(self.staging).as_posix()
        self.assertEqual((self.output / "payload" / relative).read_bytes(), original[relative])
        self.assertNotEqual(path.read_bytes(), original[relative])

    def test_incremental_publish_never_signs_or_uploads_existing_eleven(self):
        manifest = self.supplemental_manifest()
        with patch.dict(os.environ, CREDENTIALS, clear=True), \
                patch.object(release, "pending_central_coordinates", return_value=manifest["uploadCoordinates"]), \
                patch.object(release, "sign_payload") as signing, \
                patch.object(release, "publish_central", return_value=release.CENTRAL_REPOSITORY), \
                patch.object(release, "verify_remote"):
            release.publish(self.output, manifest, "darriousliu/mediamp")
        signed = signing.call_args.args[1]
        self.assertEqual({Path(name).parts[-3] for name in signed["sha256"]}, set(manifest["uploadCoordinates"]))
        result = json.loads((self.output / "result.json").read_text())
        self.assertEqual(result["uploadedCount"], 54)
        self.assertEqual(result["retainedCentralCount"], 11)

    def test_resume_skips_identical_new_coordinate(self):
        manifest = self.supplemental_manifest()
        resumed = manifest["uploadCoordinates"][0]
        with patch.object(release, "verify_remote"), \
                patch.object(release, "remote_matches", side_effect=lambda _, name, *__: Path(name).parts[-3] == resumed):
            pending = release.pending_central_coordinates(manifest)
        self.assertEqual(len(pending), 53)
        self.assertNotIn(resumed, pending)

    def test_partial_coordinate_never_uploads_or_falls_back(self):
        manifest = self.freeze()
        with patch.object(release, "remote_matches", side_effect=[True, False]), \
                patch.object(release, "verify_remote"), \
                patch.object(release, "publish_central") as central, \
                patch.object(release, "publish_packages") as fallback:
            with self.assertRaisesRegex(release.ReleaseError, "incomplete files"):
                release.publish(self.output, manifest, "darriousliu/mediamp")
            central.assert_not_called()
            fallback.assert_not_called()

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_central_credentials_never_fall_back(self):
        manifest = self.freeze()
        with patch.object(release, "publish_packages") as fallback, \
                patch.object(release, "pending_central_coordinates", return_value=manifest["uploadCoordinates"]):
            with self.assertRaisesRegex(release.ReleaseError, "credentials are missing"):
                release.publish(self.output, manifest, "darriousliu/mediamp")
            fallback.assert_not_called()

    def test_different_remote_bytes_are_never_overwritten(self):
        with patch.object(release, "request", return_value=b"old release"):
            with self.assertRaisesRegex(release.ReleaseError, "immutable coordinate"):
                release.remote_matches("https://example.test", "artifact.jar", release.digest(b"new release"), {})

    def test_remote_missing_is_distinguished_from_denied(self):
        with patch.object(release, "request", side_effect=HTTPError("https://example.test", 404, "missing", {}, None)):
            self.assertFalse(release.remote_matches("https://example.test", "artifact.jar", "unused", {}))
        with patch.object(release, "request", side_effect=HTTPError("https://example.test", 403, "denied", {}, None)):
            with self.assertRaisesRegex(release.ReleaseError, "HTTP 403"):
                release.remote_matches("https://example.test", "artifact.jar", "unused", {})

    def test_explicit_rejection_fallback_reports_split_repository_counts(self):
        manifest = self.supplemental_manifest()
        with patch.dict(os.environ, CREDENTIALS, clear=True), \
                patch.object(release, "pending_central_coordinates", return_value=manifest["uploadCoordinates"]), \
                patch.object(release, "sign_payload"), \
                patch.object(release, "publish_central", side_effect=release.CentralRejected("HTTP 429")), \
                patch.object(release, "publish_packages", return_value="https://maven.pkg.github.com/darriousliu/mediamp") as fallback, \
                patch.object(release, "verify_remote"):
            release.publish(self.output, manifest, "darriousliu/mediamp")
        fallback.assert_called_once()
        result = json.loads((self.output / "result.json").read_text())
        self.assertEqual(result["backend"], "GitHub Packages")
        self.assertEqual(result["uploadedCount"], 54)
        self.assertEqual(result["retainedCentralCount"], 11)

    def test_uncertain_central_network_failure_never_falls_back(self):
        manifest = self.freeze()
        with patch.dict(os.environ, CREDENTIALS, clear=True), \
                patch.object(release, "pending_central_coordinates", return_value=manifest["uploadCoordinates"]), \
                patch.object(release, "sign_payload"), \
                patch.object(release, "publish_central", side_effect=URLError("connection interrupted")), \
                patch.object(release, "publish_packages") as fallback:
            with self.assertRaises(URLError):
                release.publish(self.output, manifest, "darriousliu/mediamp")
            fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
