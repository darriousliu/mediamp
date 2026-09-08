#!/usr/bin/env python3
"""Freeze a desktop Maven closure, publish once, and verify every remote file.

The default release contains macOS arm64 natives only. Additional runtime
coordinates must be selected explicitly with --native-platform and already be
present in staging. No aggregate or mobile publication is synthesized here.
Secrets are read from the environment, never from command-line arguments.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid
import xml.etree.ElementTree as ET
import zipfile


GROUP = "io.github.darriousliu.mediamp"
GROUP_PATH = GROUP.replace(".", "/")
CORE = ("mediamp-api", "mediamp-internal-utils", "mediamp-native-loader", "mediamp-mpv")
CENTRAL_API = "https://central.sonatype.com/api/v1/publisher"
CENTRAL_REPOSITORY = "https://repo.maven.apache.org/maven2"
SUPPORTED_NATIVE = ("macos-arm64", "windows-x64", "windows-arm64", "macos-x64")
SECRET_NAMES = (
    "ORG_GRADLE_PROJECT_mavenCentralUsername",
    "ORG_GRADLE_PROJECT_mavenCentralPassword",
    "ORG_GRADLE_PROJECT_signingInMemoryKey",
    "ORG_GRADLE_PROJECT_signingInMemoryKeyPassword",
    "GITHUB_TOKEN",
)


class ReleaseError(RuntimeError):
    pass


class CentralRejected(ReleaseError):
    """Central has returned an explicit rejection after an HTTP request."""


def safe_message(message):
    for name in SECRET_NAMES:
        secret = os.environ.get(name, "")
        if secret and len(secret) >= 4:
            message = message.replace(secret, "[REDACTED]")
    return message


def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm, data).hexdigest()


def coordinates(platforms):
    return [*CORE, *(name + "-desktop" for name in CORE), "mediamp-mpv-tao",
            *("mediamp-mpv-runtime-" + platform for platform in platforms)]


def jni_source_hash():
    source = Path(__file__).resolve().parents[1] / "mediamp-mpv/src/cpp"
    checksum = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file():
            checksum.update(str(path.relative_to(source)).encode())
            checksum.update(path.read_bytes())
    return checksum.hexdigest()


def validate_module(path, expected, version):
    module = json.loads(path.read_text())
    component = module.get("component", {})
    if component.get("group") != GROUP or component.get("version") != version:
        raise ReleaseError(f"Unexpected Gradle module coordinates: {path.name}")
    for variant in module.get("variants", []):
        target = variant.get("available-at")
        if target and variant["name"].startswith("desktop"):
            if target.get("group") != GROUP or target.get("module") not in expected or target.get("version") != version:
                raise ReleaseError(f"Unpublished desktop variant in {path.name}")
        for dependency in variant.get("dependencies", []):
            if dependency.get("group") == "org.openani.mediamp":
                raise ReleaseError(f"Fork metadata still depends on the upstream namespace: {path.name}")
            if dependency.get("group") != GROUP:
                continue
            dep_version = dependency.get("version", {})
            if dependency.get("module") not in expected or dep_version.get("requires") != version:
                raise ReleaseError(f"Unpublished Gradle dependency in {path.name}")
        for artifact in variant.get("files", []):
            relative = Path(artifact["url"])
            if relative.is_absolute() or len(relative.parts) != 1:
                raise ReleaseError(f"Unsafe Gradle artifact URL in {path.name}")
            referenced = path.parent / relative
            if not referenced.is_file() or digest(referenced.read_bytes()) != artifact.get("sha256"):
                raise ReleaseError(f"Gradle metadata checksum mismatch in {path.name}: {relative}")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def freeze(staging, output, version, platforms):
    if (output / "payload").exists():
        raise ReleaseError("Output already contains a payload; use a fresh output directory.")
    expected = coordinates(platforms)
    expected_jni_hash = jni_source_hash()
    output.mkdir(parents=True, exist_ok=True)
    records = {}
    for artifact in expected:
        relative = Path(GROUP_PATH) / artifact / version
        source = staging / relative
        prefix = f"{artifact}-{version}"
        required = [prefix + ".pom", prefix + ".jar", prefix + "-javadoc.jar", prefix + "-sources.jar"]
        if not artifact.startswith("mediamp-mpv-runtime-"):
            required += [prefix + ".module"]
        for name in required:
            if not (source / name).is_file():
                raise ReleaseError(f"Incomplete staging: missing {relative / name}")
        pom = ET.parse(source / (prefix + ".pom")).getroot()
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        for field, wanted in (("groupId", GROUP), ("artifactId", artifact), ("version", version)):
            if pom.findtext("m:" + field, namespaces=ns) != wanted:
                raise ReleaseError(f"Unexpected POM {field}: {relative}")
        for dependency in pom.findall("m:dependencies/m:dependency", ns):
            if dependency.findtext("m:groupId", namespaces=ns) != GROUP:
                continue
            dep_artifact = dependency.findtext("m:artifactId", namespaces=ns)
            dep_version = dependency.findtext("m:version", namespaces=ns)
            if dep_artifact not in expected or dep_version not in (version, f"[{version}]"):
                raise ReleaseError(f"Unpublished dependency in {artifact}: {dep_artifact}:{dep_version}")
        for path in sorted(source.iterdir()):
            # Ignore Maven-local bookkeeping, stale signatures and regenerated checksums.
            if not path.name.startswith(prefix) or path.suffix not in (".jar", ".pom", ".module", ".json"):
                continue
            if path.is_symlink() or not path.is_file():
                raise ReleaseError(f"Unexpected staged file: {path.name}")
            data = path.read_bytes()
            if path.suffix == ".module":
                validate_module(path, expected, version)
            if path.suffix == ".jar":
                with zipfile.ZipFile(path) as archive:
                    if archive.testzip() is not None:
                        raise ReleaseError(f"Corrupt JAR: {path.name}")
                    if artifact.startswith("mediamp-mpv-runtime-") and path.name == prefix + ".jar":
                        provenance = dict(
                            line.split("=", 1)
                            for line in archive.read("META-INF/mediamp-tao-native-build.txt").decode().splitlines()
                            if "=" in line
                        )
                        if provenance.get("version") != version or provenance.get("base-runtime") != "0.4.0":
                            raise ReleaseError(f"Unverified native runtime provenance: {artifact}")
                        if provenance.get("jni-source-sha256") != expected_jni_hash:
                            raise ReleaseError(f"Native runtime does not match this checkout's JNI source: {artifact}")
                        platform = artifact.removeprefix("mediamp-mpv-runtime-")
                        if f"mpv-natives-{platform}.txt" not in archive.namelist():
                            raise ReleaseError(f"Missing platform native manifest: {artifact}")
            target = output / "payload" / relative / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            records[target.relative_to(output / "payload").as_posix()] = digest(data)
    write_json(output / "manifest.json", {
        "version": version, "group": GROUP, "nativePlatforms": platforms,
        "coordinates": expected, "sha256": records,
    })
    print(f"Frozen {len(expected)} coordinates, {len(records)} files; natives: {', '.join(platforms)}")


def checked_manifest(output, version, platforms):
    manifest = json.loads((output / "manifest.json").read_text())
    if manifest["version"] != version or manifest["nativePlatforms"] != platforms:
        raise ReleaseError("Frozen manifest does not match the requested version/platforms.")
    if manifest["coordinates"] != coordinates(platforms):
        raise ReleaseError("Frozen coordinate set has changed.")
    for relative, wanted in manifest["sha256"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ReleaseError("Unsafe frozen artifact path.")
        if digest((output / "payload" / path).read_bytes()) != wanted:
            raise ReleaseError(f"Frozen artifact has changed: {relative}")
    return manifest


def request(url, method="GET", data=None, headers=None):
    with urlopen(Request(url, data=data, headers=headers or {}, method=method), timeout=120) as response:
        return response.read()


def remote_matches(base, relative, wanted, headers):
    try:
        data = request(base + "/" + quote(relative, safe="/"), headers=headers)
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 404:
            return False
        raise ReleaseError(f"Remote verification rejected {relative}: HTTP {status}") from None
    if digest(data) != wanted:
        raise ReleaseError(f"Remote content differs for immutable coordinate: {relative}")
    return True


def verify_remote(base, records, headers, wait_seconds=0):
    pending = dict(records)
    deadline = time.monotonic() + wait_seconds
    while pending:
        for relative, wanted in list(pending.items()):
            if remote_matches(base, relative, wanted, headers):
                del pending[relative]
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise ReleaseError(f"Remote publication is incomplete: {len(pending)} files missing.")
        time.sleep(15)


def central_already_published(manifest):
    probes = []
    for artifact in manifest["coordinates"]:
        prefix = f"{GROUP_PATH}/{artifact}/{manifest['version']}/{artifact}-{manifest['version']}"
        probes += [prefix + ".pom", prefix + ".jar"]
    present = [remote_matches(CENTRAL_REPOSITORY, relative, manifest["sha256"][relative], {})
               for relative in probes]
    if not any(present):
        return False
    if not all(present):
        raise ReleaseError("Central already contains a partial release of this version; no upload or fallback attempted.")
    verify_remote(CENTRAL_REPOSITORY, manifest["sha256"], {})
    return True


def sign_payload(output, manifest):
    key = os.environ.get("ORG_GRADLE_PROJECT_signingInMemoryKey", "")
    if not key.strip():
        raise ReleaseError("Central signing key is missing; no upload or fallback was attempted.")
    if not shutil.which("gpg"):
        raise ReleaseError("gpg is required for Central publication.")
    with tempfile.TemporaryDirectory(prefix="tao-release-gpg-") as temporary:
        os.chmod(temporary, 0o700)
        command = ["gpg", "--homedir", temporary, "--batch", "--no-tty"]
        imported = subprocess.run(command + ["--import"], input=key.encode(), capture_output=True)
        if imported.returncode:
            raise ReleaseError("Cannot import Central signing key; no upload or fallback was attempted.")
        password = os.environ.get("ORG_GRADLE_PROJECT_signingInMemoryKeyPassword", "")
        key_id = os.environ.get("ORG_GRADLE_PROJECT_signingInMemoryKeyId", "").strip()
        for relative in manifest["sha256"]:
            path = output / "payload" / relative
            signing = command + ["--yes", "--pinentry-mode", "loopback", "--passphrase-fd", "0"]
            if key_id:
                signing += ["--local-user", key_id]
            signed = subprocess.run(signing + ["--armor", "--detach-sign", str(path)],
                                    input=(password + "\n").encode(), capture_output=True)
            if signed.returncode:
                raise ReleaseError("Central artifact signing failed; no upload or fallback was attempted.")
            verified = subprocess.run(command + ["--verify", str(path) + ".asc", str(path)], capture_output=True)
            if verified.returncode:
                raise ReleaseError("Generated artifact signature did not verify.")
        subprocess.run(["gpgconf", "--homedir", temporary, "--kill", "all"], capture_output=True)


def all_publication_files(output, manifest):
    records = dict(manifest["sha256"])
    for relative in list(records):
        signature = output / "payload" / (relative + ".asc")
        if signature.is_file():
            records[relative + ".asc"] = digest(signature.read_bytes())
    for relative in list(records):
        data = (output / "payload" / relative).read_bytes()
        for algorithm in ("sha1", "md5"):
            checksum = digest(data, algorithm).encode() + b"\n"
            (output / "payload" / (relative + "." + algorithm)).write_bytes(checksum)
            records[relative + "." + algorithm] = digest(checksum)
    return records


def publish_central(output, records, authorization):
    bundle = output / "central-bundle.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in records:
            archive.write(output / "payload" / relative, relative)
    boundary = "tao-" + uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"bundle\"; "
            f"filename=\"central-bundle.zip\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    body += bundle.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Authorization": authorization, "Content-Type": "multipart/form-data; boundary=" + boundary}
    try:
        deployment = request(CENTRAL_API + "/upload?publishingType=AUTOMATIC", "POST", body, headers).decode().strip()
    except HTTPError as error:
        details = safe_message(error.read(8192).decode(errors="replace"))[:4000]
        status = error.code
        error.close()
        write_json(output / "central-error.json", {"phase": "upload", "httpStatus": status, "details": details})
        message = f"Central upload rejected: HTTP {status}; {details}"
        if 400 <= status < 500 and status != 408:
            raise CentralRejected(message) from None
        raise ReleaseError(message + "; upload state is uncertain, no fallback attempted.") from None
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", deployment):
        raise ReleaseError("Central returned an unexpected upload result; publication state is unknown.")
    write_json(output / "central-deployment.json", {"deploymentId": deployment, "state": "PENDING"})
    print(f"Central accepted deployment {deployment}; waiting for PUBLISHED.")
    deadline = time.monotonic() + 2700
    while time.monotonic() < deadline:
        # A transport or HTTP error here is ambiguous: Central may still publish.
        # Preserve the deployment ID and stop instead of creating a second release.
        status = json.loads(request(CENTRAL_API + "/status?id=" + deployment, "POST", b"",
                                    {"Authorization": authorization}))
        state = status.get("deploymentState", "UNKNOWN")
        write_json(output / "central-deployment.json", {"deploymentId": deployment, "state": state})
        if state == "FAILED":
            errors = safe_message(json.dumps(status.get("errors", {}), ensure_ascii=False))
            raise CentralRejected("Central deployment validation failed: " + errors)
        if state == "PUBLISHED":
            # Central may omit redundant checksum files; verify every payload and signature.
            actual = {name: sha for name, sha in records.items() if not name.endswith((".sha1", ".md5"))}
            verify_remote(CENTRAL_REPOSITORY, actual, {}, wait_seconds=600)
            return
        time.sleep(15)
    raise ReleaseError("Central is still processing. Inspect central-deployment.json before retrying; no fallback attempted.")


def publish_packages(output, records, repository):
    token = os.environ.get("GITHUB_TOKEN", "")
    actor = os.environ.get("GITHUB_ACTOR", "")
    if not token or not actor:
        raise ReleaseError("Packages fallback requires GITHUB_TOKEN and GITHUB_ACTOR.")
    base = "https://maven.pkg.github.com/" + repository.lower()
    headers = {"Authorization": "Basic " + base64.b64encode(f"{actor}:{token}".encode()).decode()}
    for relative, wanted in records.items():
        if remote_matches(base, relative, wanted, headers):
            continue
        try:
            request(base + "/" + quote(relative, safe="/"), "PUT", (output / "payload" / relative).read_bytes(),
                    {**headers, "Content-Type": "application/octet-stream"})
        except HTTPError as error:
            # A rerun can race an earlier upload; only accept exact matching bytes.
            status = error.code
            error.close()
            if status not in (409, 422) or not remote_matches(base, relative, wanted, headers):
                raise ReleaseError(f"Packages upload rejected {relative}: HTTP {status}") from None
    verify_remote(base, records, headers, wait_seconds=120)
    return base


def publish(output, manifest, repository):
    if central_already_published(manifest):
        record_success(output, manifest, "Maven Central", CENTRAL_REPOSITORY, already_published=True)
        return
    username = os.environ.get("ORG_GRADLE_PROJECT_mavenCentralUsername", "")
    password = os.environ.get("ORG_GRADLE_PROJECT_mavenCentralPassword", "")
    if not username.strip() or not password.strip():
        raise ReleaseError("Central credentials are missing; no Central request or Packages fallback was attempted.")
    sign_payload(output, manifest)
    records = all_publication_files(output, manifest)
    write_json(output / "publication-files.json", records)
    authorization = "Bearer " + base64.b64encode(f"{username}:{password}".encode()).decode()
    fallback_reason = None
    try:
        publish_central(output, records, authorization)
        backend, repository_url = "Maven Central", CENTRAL_REPOSITORY
    except CentralRejected as error:
        fallback_reason = safe_message(str(error))
        print(fallback_reason)
        print("Central explicitly rejected the publication; publishing the same frozen files to GitHub Packages.")
        repository_url = publish_packages(output, records, repository)
        backend = "GitHub Packages"
    record_success(output, manifest, backend, repository_url, fallback_reason)


def record_success(output, manifest, backend, repository_url, fallback_reason=None, already_published=False):
    result = {"version": manifest["version"], "backend": backend, "repository": repository_url,
              "nativePlatforms": manifest["nativePlatforms"], "coordinates": manifest["coordinates"],
              "verified": True, "fallbackReason": fallback_reason, "alreadyPublished": already_published}
    write_json(output / "result.json", result)
    message = (f"Published and verified {len(manifest['coordinates'])} coordinates to {backend}. "
               f"Native platforms: {', '.join(manifest['nativePlatforms'])}. "
               "This is a desktop-only publication; no mobile or all-platform runtime release is implied.")
    print(message)
    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(message + "\n\nRepository: " + repository_url + "\n")
            if backend == "GitHub Packages":
                summary.write("\nConsumers require GitHub Packages read authentication, including for public Maven packages.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--native-platform", action="append", choices=SUPPORTED_NATIVE, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "darriousliu/mediamp"))
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+-tao(?:[.-][A-Za-z0-9.-]+)?", args.version):
        parser.error("Only explicit -tao release versions are accepted.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Expected a GitHub owner/repository name.")
    if len(set(args.native_platform)) != len(args.native_platform):
        parser.error("Duplicate native platforms are not allowed.")
    if args.prepare_only:
        if args.staging is None:
            parser.error("--prepare-only requires --staging.")
        freeze(args.staging, args.output, args.version, args.native_platform)
    else:
        manifest = checked_manifest(args.output, args.version, args.native_platform)
        publish(args.output, manifest, args.repository)


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, HTTPError, URLError, OSError, ValueError, KeyError) as error:
        print("Release failed: " + safe_message(str(error)), file=sys.stderr)
        sys.exit(1)
