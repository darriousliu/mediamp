"""Offline release checks: closure completeness, immutable bytes and fallback gating."""

import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import zipfile


spec = importlib.util.spec_from_file_location("publish_tao", Path(__file__).with_name("publish-tao-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

VERSION = "0.4.0-tao"
PLATFORMS = ["macos-arm64", "windows-x64"]


def jar(entries=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, contents in (entries or {"marker": "fixture"}).items():
            archive.writestr(name, contents)
    return buffer.getvalue()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.staging, self.output = self.root / "staging", self.root / "output"
        for artifact in release.coordinates(PLATFORMS):
            directory = self.staging / release.GROUP_PATH / artifact / VERSION
            directory.mkdir(parents=True)
            prefix = artifact + "-" + VERSION
            (directory / (prefix + ".pom")).write_text(
                '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
                f"<groupId>{release.GROUP}</groupId><artifactId>{artifact}</artifactId>"
                f"<version>{VERSION}</version></project>")
            content = jar()
            if artifact.startswith("mediamp-mpv-runtime-"):
                platform = artifact.removeprefix("mediamp-mpv-runtime-")
                content = jar({"META-INF/mediamp-tao-native-build.txt":
                               f"version={VERSION}\nbase-runtime=0.4.0\njni-source-sha256={release.jni_source_hash()}\n",
                               f"mpv-natives-{platform}.txt": "libmediampv.dylib\n"})
            else:
                (directory / (prefix + ".module")).write_text(json.dumps({"formatVersion": "1.1",
                    "component": {"group": release.GROUP, "module": artifact, "version": VERSION}}))
            for suffix in ("-sources.jar", "-javadoc.jar"):
                (directory / (prefix + suffix)).write_bytes(jar())
            (directory / (prefix + ".jar")).write_bytes(content)

    def freeze(self):
        release.freeze(self.staging, self.output, VERSION, PLATFORMS)
        return release.checked_manifest(self.output, VERSION, PLATFORMS)

    def test_complete_eleven_coordinate_closure_and_no_aggregator(self):
        manifest = self.freeze()
        self.assertEqual(len(manifest["coordinates"]), 11)
        self.assertNotIn("mediamp-mpv-runtime", manifest["coordinates"])
        self.assertEqual(manifest["nativePlatforms"], PLATFORMS)

    def test_missing_desktop_module_cannot_freeze(self):
        path = self.staging / release.GROUP_PATH / "mediamp-mpv-desktop" / VERSION
        (path / ("mediamp-mpv-desktop-" + VERSION + ".module")).unlink()
        with self.assertRaisesRegex(release.ReleaseError, "Incomplete staging"):
            self.freeze()

    def test_windows_crlf_native_provenance_is_accepted(self):
        artifact = "mediamp-mpv-runtime-windows-x64"
        path = self.staging / release.GROUP_PATH / artifact / VERSION / (artifact + "-" + VERSION + ".jar")
        with zipfile.ZipFile(path) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        name = "META-INF/mediamp-tao-native-build.txt"
        contents[name] = contents[name].replace(b"\n", b"\r\n")
        path.write_bytes(jar(contents))
        self.assertEqual(len(self.freeze()["coordinates"]), 11)

    def test_unpublished_same_group_dependency_cannot_freeze(self):
        path = self.staging / release.GROUP_PATH / "mediamp-mpv-tao" / VERSION / ("mediamp-mpv-tao-" + VERSION + ".pom")
        path.write_text(path.read_text().replace("</project>", "<dependencies><dependency>"
                        f"<groupId>{release.GROUP}</groupId><artifactId>missing-module</artifactId>"
                        f"<version>{VERSION}</version></dependency></dependencies></project>"))
        with self.assertRaisesRegex(release.ReleaseError, "Unpublished dependency"):
            self.freeze()

    def test_frozen_file_tampering_is_rejected(self):
        manifest = self.freeze()
        path = self.output / "payload" / next(iter(manifest["sha256"]))
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(release.ReleaseError, "has changed"):
            release.checked_manifest(self.output, VERSION, PLATFORMS)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_central_credentials_never_fall_back(self):
        manifest = self.freeze()
        with patch.object(release, "publish_packages") as fallback, \
                patch.object(release, "central_already_published", return_value=False):
            with self.assertRaisesRegex(release.ReleaseError, "credentials are missing"):
                release.publish(self.output, manifest, "darriousliu/mediamp")
            fallback.assert_not_called()

    def publish_with_mock_central(self, error):
        manifest = self.freeze()
        environment = {"ORG_GRADLE_PROJECT_mavenCentralUsername": "fixture-user",
                       "ORG_GRADLE_PROJECT_mavenCentralPassword": "fixture-password"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(release, "central_already_published", return_value=False), \
                patch.object(release, "sign_payload"), \
                patch.object(release, "publish_central", side_effect=error), \
                patch.object(release, "publish_packages", return_value="https://maven.pkg.github.com/darriousliu/mediamp") as fallback:
            if isinstance(error, release.CentralRejected):
                release.publish(self.output, manifest, "darriousliu/mediamp")
                fallback.assert_called_once()
                result = json.loads((self.output / "result.json").read_text())
                self.assertEqual(result["backend"], "GitHub Packages")
            else:
                with self.assertRaises(type(error)):
                    release.publish(self.output, manifest, "darriousliu/mediamp")
                fallback.assert_not_called()

    def test_explicit_central_rejection_can_fall_back(self):
        self.publish_with_mock_central(release.CentralRejected("HTTP 429"))

    def test_uncertain_central_network_failure_never_falls_back(self):
        self.publish_with_mock_central(URLError("connection interrupted"))

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

    def test_partial_central_release_never_uploads_or_falls_back(self):
        manifest = self.freeze()
        matches = [True] + [False] * (len(manifest["coordinates"]) * 2 - 1)
        with patch.object(release, "remote_matches", side_effect=matches), \
                patch.object(release, "publish_central") as central, \
                patch.object(release, "publish_packages") as fallback:
            with self.assertRaisesRegex(release.ReleaseError, "partial release"):
                release.publish(self.output, manifest, "darriousliu/mediamp")
            central.assert_not_called()
            fallback.assert_not_called()

    def test_identical_existing_central_release_is_verified_without_republishing(self):
        manifest = self.freeze()
        with patch.object(release, "remote_matches", return_value=True), \
                patch.object(release, "verify_remote") as verification, \
                patch.object(release, "publish_central") as central, \
                patch.object(release, "publish_packages") as fallback:
            release.publish(self.output, manifest, "darriousliu/mediamp")
            verification.assert_called_once_with(release.CENTRAL_REPOSITORY, manifest["sha256"], {})
            central.assert_not_called()
            fallback.assert_not_called()

    def test_fork_module_must_not_redirect_to_upstream_dependencies(self):
        path = self.staging / release.GROUP_PATH / "mediamp-mpv-tao" / VERSION / ("mediamp-mpv-tao-" + VERSION + ".module")
        module = json.loads(path.read_text())
        module["variants"] = [{"name": "runtimeElements", "dependencies": [
            {"group": "org.openani.mediamp", "module": "mediamp-mpv", "version": {"requires": VERSION}}]}]
        path.write_text(json.dumps(module))
        with self.assertRaisesRegex(release.ReleaseError, "upstream namespace"):
            self.freeze()


if __name__ == "__main__":
    unittest.main()
