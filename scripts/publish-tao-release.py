#!/usr/bin/env python3
"""Freeze all declared MediaMP publications and verify their complete closure.

The checked-in inventory includes Android, iOS, Wasm, JVM, all five desktop
runtime targets, and the FFmpeg XCFramework. Secrets are read only from env.
"""

import argparse
import base64
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((Path(__file__).with_name("tao-publications.json")).read_text())
GROUP = INVENTORY["group"]
GROUP_PATH = GROUP.replace(".", "/")
CENTRAL_API = "https://central.sonatype.com/api/v1/publisher"
CENTRAL_REPOSITORY = "https://repo.maven.apache.org/maven2"
SUPPORTED_NATIVE = tuple(INVENTORY["nativePlatforms"])
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


def publication_specs():
    specs = {}
    for module, targets in INVENTORY["multiplatform"].items():
        specs[module] = "metadata"
        for target in targets:
            specs[f"{module}-{target}"] = {
                "android": "android", "desktop": "jvm", "iosarm64": "native",
                "iossimulatorarm64": "native", "wasm-js": "wasm",
            }[target]
    specs.update(INVENTORY["standalone"])
    for family in INVENTORY["nativeFamilies"]:
        specs[f"mediamp-{family}-runtime"] = "aggregate"
        for platform in SUPPORTED_NATIVE:
            specs[f"mediamp-{family}-runtime-{platform}"] = "runtime"
    specs[INVENTORY["xcframework"]] = "xcframework"
    return specs


def coordinates(platforms=None):
    if platforms is not None and (len(platforms) != len(SUPPORTED_NATIVE) or set(platforms) != set(SUPPORTED_NATIVE)):
        raise ReleaseError("A full release requires every native platform in tao-publications.json.")
    return list(publication_specs())


def primary_extension(kind):
    return {"metadata": ".jar", "jvm": ".jar", "android": ".aar", "native": ".klib",
            "wasm": ".klib", "runtime": ".jar", "xcframework": ".zip", "catalog": ".toml",
            "aggregate": ".pom"}[kind]


def required_suffixes(kind):
    required = {".pom", primary_extension(kind)}
    if kind not in ("runtime", "xcframework"):
        required.add(".module")
    if kind != "catalog":
        required.add("-javadoc.jar")
    if kind in ("metadata", "jvm", "android", "native", "wasm", "runtime"):
        required.add("-sources.jar")
    return required


def jni_source_hash():
    source = ROOT / "mediamp-mpv/src/cpp"
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
        if target:
            if target.get("group") != GROUP or target.get("module") not in expected or target.get("version") != version:
                raise ReleaseError(f"Unpublished platform variant in {path.name}")
            redirected = (path.parent / target["url"]).resolve()
            wanted = path.parent.parent.parent / target["module"] / version / f"{target['module']}-{version}.module"
            if redirected != wanted.resolve() or not redirected.is_file():
                raise ReleaseError(f"Broken available-at URL in {path.name}")
        for dependency in variant.get("dependencies", []) + variant.get("dependencyConstraints", []):
            if dependency.get("group") == "org.openani.mediamp":
                raise ReleaseError(f"Fork metadata still depends on the upstream namespace: {path.name}")
            if dependency.get("group") != GROUP:
                continue
            dep_version = dependency.get("version", {})
            if dependency.get("module") not in expected or dep_version.get("requires", dep_version.get("strictly")) != version:
                raise ReleaseError(f"Unpublished Gradle dependency in {path.name}")
        for artifact in variant.get("files", []):
            relative = Path(artifact["url"])
            if relative.is_absolute() or len(relative.parts) != 1:
                raise ReleaseError(f"Unsafe Gradle artifact URL in {path.name}")
            referenced = path.parent / relative
            if not referenced.is_file() or digest(referenced.read_bytes()) != artifact.get("sha256"):
                raise ReleaseError(f"Gradle metadata checksum mismatch in {path.name}: {relative}")


def validate_elf(data, abi, label):
    expected = {"armeabi-v7a": (1, 40), "arm64-v8a": (2, 183), "x86": (1, 3), "x86_64": (2, 62)}[abi]
    if len(data) < 20 or data[:4] != b"\x7fELF" or data[5] != 1:
        raise ReleaseError(f"Missing ELF binary: {label}")
    if (data[4], int.from_bytes(data[18:20], "little")) != expected:
        raise ReleaseError(f"Wrong ELF architecture: {label}")


def validate_android(archive, artifact):
    wrappers = {"mediamp-mpv-android": "libmediampv.so", "mediamp-ffmpeg-android": "libffmpegkitjni.so",
                "mediamp-exoplayer-android": "libmediamp_wsola.so"}
    if artifact not in wrappers:
        return
    for abi in INVENTORY["androidAbis"]:
        name = f"jni/{abi}/{wrappers[artifact]}"
        if name not in archive.namelist():
            raise ReleaseError(f"Android native backend is missing: {artifact}/{name}")
        validate_elf(archive.read(name), abi, f"{artifact}/{name}")
        if artifact != "mediamp-exoplayer-android":
            libraries = ["libavcodec.so", "libavformat.so", "libavutil.so"]
            if artifact == "mediamp-mpv-android":
                libraries += ["libmpv.so", "libc++_shared.so"]
            for library in libraries:
                dependency = f"jni/{abi}/{library}"
                if dependency not in archive.namelist():
                    raise ReleaseError(f"Android codec dependency is missing: {artifact}/{dependency}")
                validate_elf(archive.read(dependency), abi, f"{artifact}/{dependency}")
    if artifact == "mediamp-exoplayer-android":
        import io
        with zipfile.ZipFile(io.BytesIO(archive.read("classes.jar"))) as classes:
            license_path = "META-INF/licenses/org.openani.mediamp/mediamp-exoplayer/scaletempo2.txt"
            if license_path not in classes.namelist():
                raise ReleaseError("ExoPlayer AAR is missing the scaletempo2 license.")


def validate_xcframework(archive):
    info_names = [name for name in archive.namelist() if name.endswith(".xcframework/Info.plist")]
    if len(info_names) != 1:
        raise ReleaseError("Expected one FFmpeg XCFramework Info.plist.")
    root = info_names[0].removesuffix("Info.plist")
    info = plistlib.loads(archive.read(info_names[0]))
    variants = set()
    for library in info.get("AvailableLibraries", []):
        if library.get("SupportedPlatform") != "ios" or "arm64" not in library.get("SupportedArchitectures", []):
            continue
        variants.add(library.get("SupportedPlatformVariant", "device"))
        prefix = root + library["LibraryIdentifier"] + "/" + library["LibraryPath"].rstrip("/") + "/"
        if not any(name.startswith(prefix) and name.endswith("MediampFFmpegKit") and archive.getinfo(name).file_size > 0
                   for name in archive.namelist()):
            raise ReleaseError("FFmpeg XCFramework slice is missing its linked library.")
    if variants != {"device", "simulator"}:
        raise ReleaseError("FFmpeg XCFramework must include arm64 iOS device and simulator slices.")


def validate_runtime(archive, artifact, version, already_published=False):
    family = "mpv" if artifact.startswith("mediamp-mpv-") else "ffmpeg"
    platform = artifact.removeprefix(f"mediamp-{family}-runtime-")
    provenance = dict(line.split("=", 1) for line in
                      archive.read("META-INF/mediamp-tao-native-build.txt").decode().splitlines() if "=" in line)
    if provenance.get("version") != version or provenance.get("base-runtime") != "0.4.0":
        raise ReleaseError(f"Unverified native runtime provenance: {artifact}")
    if family == "mpv" and not already_published and provenance.get("jni-source-sha256") != jni_source_hash():
        raise ReleaseError(f"Native runtime does not match this checkout's JNI source: {artifact}")
    if family == "ffmpeg":
        # Imported lazily because the helper also supports platform build commands.
        from tao_runtime import ffmpeg_compatibility_source_hash
        if provenance.get("jni") != "reused-upstream-unchanged" or provenance.get("compatibility-source-sha256") != ffmpeg_compatibility_source_hash():
            raise ReleaseError(f"FFmpeg native provenance does not match the verified upstream sources: {artifact}")
    manifest_name = f"mpv-natives-{platform}.txt" if family == "mpv" else "ffmpeg-natives.txt"
    if manifest_name not in archive.namelist():
        raise ReleaseError(f"Missing platform native manifest: {artifact}")
    libraries = [line.strip() for line in archive.read(manifest_name).decode().splitlines() if line.strip()]
    for name in libraries:
        if Path(name).is_absolute() or ".." in Path(name).parts or name not in archive.namelist() or archive.getinfo(name).file_size == 0:
            raise ReleaseError(f"Broken native dependency manifest: {artifact}/{name}")
    wrapper = {"mpv": {"windows": "mediampv.dll", "macos": "libmediampv.dylib", "linux": "libmediampv.so"},
               "ffmpeg": {"windows": "ffmpegkitjni.dll", "macos": "libffmpegkitjni.dylib", "linux": "libffmpegkitjni.so"}}[family][platform.split("-")[0]]
    if not any(Path(name).name == wrapper for name in libraries):
        raise ReleaseError(f"Native runtime manifest omits its JNI wrapper: {artifact}")
    if not already_published:
        from tao_runtime import BASE_SHA256
        wrappers = [name for name in libraries if Path(name).name == wrapper]
        if provenance.get("base-sha256") != BASE_SHA256[family][platform]:
            raise ReleaseError(f"Native runtime has the wrong verified upstream input: {artifact}")
        if len(wrappers) != 1 or provenance.get("jni-binary-sha256") != digest(archive.read(wrappers[0])):
            raise ReleaseError(f"Native runtime JNI binary checksum mismatch: {artifact}")


def validate_catalog(path, expected, version):
    catalog = tomllib.loads(path.read_text())
    versions = catalog.get("versions", {})
    for alias, library in catalog.get("libraries", {}).items():
        group, module = library["module"].split(":") if "module" in library else (library["group"], library["name"])
        selected_version = library.get("version")
        if isinstance(selected_version, dict):
            selected_version = versions.get(selected_version.get("ref"))
        if group != GROUP or module not in expected or selected_version != version:
            raise ReleaseError(f"Catalog alias points outside the complete fork release: {alias}")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


ARTIFACT_EXTENSIONS = {".jar", ".aar", ".klib", ".zip", ".toml", ".pom", ".module", ".json"}


class DirectoryLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links += [value for key, value in attrs if key == "href"]


def overlay_existing(staging, output, version, expected):
    """Copy into an isolated tree and replace existing GAVs with Central's exact files."""
    resolved = output / "resolved-staging"
    existing = []
    remote_hashes = {}
    for artifact in expected:
        relative = Path(GROUP_PATH) / artifact / version
        target = resolved / relative
        target.mkdir(parents=True, exist_ok=True)
        prefix = f"{artifact}-{version}"
        base = CENTRAL_REPOSITORY + "/" + relative.as_posix()
        try:
            pom = request(base + "/" + prefix + ".pom")
        except HTTPError as error:
            status = error.code
            error.close()
            if status != 404:
                raise ReleaseError(f"Cannot inspect existing {artifact}: HTTP {status}") from None
            source = staging / relative
            if not source.is_dir():
                raise ReleaseError(f"Missing unpublished staged coordinate: {artifact}")
            shutil.copytree(source, target, dirs_exist_ok=True)
            continue
        links = DirectoryLinks()
        links.feed(request(base + "/").decode())
        files = [name for name in links.links if name.startswith(prefix) and "/" not in name
                 and Path(name).suffix in ARTIFACT_EXTENSIONS]
        if prefix + ".pom" not in files:
            raise ReleaseError(f"Cannot enumerate the immutable Central files for {artifact}")
        for name in files:
            data = pom if name == prefix + ".pom" else request(base + "/" + quote(name))
            (target / name).write_bytes(data)
            remote_hashes[(relative / name).as_posix()] = digest(data)
        existing.append(artifact)
    write_json(output / "existing-central-files.json", remote_hashes)
    return resolved, existing, remote_hashes


def freeze(staging, output, version, platforms=None, supplement_existing=False):
    if (output / "payload").exists():
        raise ReleaseError("Output already contains a payload; use a fresh output directory.")
    expected = coordinates(platforms)
    specs = publication_specs()
    output.mkdir(parents=True, exist_ok=True)
    existing, existing_hashes = [], {}
    if supplement_existing:
        staging, existing, existing_hashes = overlay_existing(staging, output, version, expected)
    records = {}
    for artifact in expected:
        relative = Path(GROUP_PATH) / artifact / version
        source = staging / relative
        prefix = f"{artifact}-{version}"
        required = [prefix + suffix for suffix in required_suffixes(specs[artifact])]
        for name in required:
            if not (source / name).is_file():
                raise ReleaseError(f"Incomplete staging: missing {relative / name}")
        pom = ET.parse(source / (prefix + ".pom")).getroot()
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        for field, wanted in (("groupId", GROUP), ("artifactId", artifact), ("version", version)):
            if pom.findtext("m:" + field, namespaces=ns) != wanted:
                raise ReleaseError(f"Unexpected POM {field}: {relative}")
        for dependency in pom.findall(".//m:dependency", ns):
            dep_group = dependency.findtext("m:groupId", namespaces=ns)
            if dep_group == "org.openani.mediamp":
                raise ReleaseError(f"Fork POM still depends on the upstream namespace: {artifact}")
            if dep_group != GROUP:
                continue
            dep_artifact = dependency.findtext("m:artifactId", namespaces=ns)
            dep_version = dependency.findtext("m:version", namespaces=ns)
            if dep_artifact not in expected or dep_version not in (version, f"[{version}]"):
                raise ReleaseError(f"Unpublished dependency in {artifact}: {dep_artifact}:{dep_version}")
        for path in sorted(source.iterdir()):
            # Ignore Maven-local bookkeeping, stale signatures and regenerated checksums.
            if not path.name.startswith(prefix) or path.suffix not in ARTIFACT_EXTENSIONS:
                continue
            if path.is_symlink() or not path.is_file():
                raise ReleaseError(f"Unexpected staged file: {path.name}")
            data = path.read_bytes()
            if path.suffix == ".module":
                validate_module(path, expected, version)
            if path.suffix == ".toml":
                validate_catalog(path, expected, version)
            if path.suffix in (".jar", ".aar", ".klib", ".zip"):
                with zipfile.ZipFile(path) as archive:
                    if archive.testzip() is not None:
                        raise ReleaseError(f"Corrupt archive: {path.name}")
                    if specs[artifact] == "runtime" and path.name == prefix + ".jar":
                        validate_runtime(archive, artifact, version, artifact in existing)
                    if path.suffix == ".aar":
                        validate_android(archive, artifact)
                    if specs[artifact] == "xcframework" and path.name == prefix + ".zip":
                        validate_xcframework(archive)
            target = output / "payload" / relative / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            records[target.relative_to(output / "payload").as_posix()] = digest(data)
    write_json(output / "manifest.json", {
        "version": version, "group": GROUP, "nativePlatforms": list(SUPPORTED_NATIVE),
        "coordinates": expected, "sha256": records,
        "supplementExisting": supplement_existing, "existingCoordinates": existing,
        "existingSha256": existing_hashes,
        "uploadCoordinates": [name for name in expected if name not in existing],
    })
    print(f"Frozen {len(expected)} coordinates, {len(records)} files; "
          f"{len(existing)} immutable Central coordinates retained, {len(expected) - len(existing)} to upload.")


def checked_manifest(output, version, platforms=None, supplement_existing=False):
    manifest = json.loads((output / "manifest.json").read_text())
    coordinates(platforms)
    if manifest["version"] != version or manifest["nativePlatforms"] != list(SUPPORTED_NATIVE):
        raise ReleaseError("Frozen manifest does not match the requested version/platforms.")
    if manifest["coordinates"] != coordinates() or manifest["supplementExisting"] != supplement_existing:
        raise ReleaseError("Frozen coordinate set has changed.")
    existing, uploads = set(manifest["existingCoordinates"]), set(manifest["uploadCoordinates"])
    if existing & uploads or existing | uploads != set(coordinates()):
        raise ReleaseError("Invalid existing/upload coordinate partition.")
    if manifest["existingSha256"] != records_for_coordinates(manifest, existing):
        raise ReleaseError("Immutable Central file evidence does not match the frozen payload.")
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


def records_for_coordinates(manifest, selected):
    selected = set(selected)
    return {relative: sha for relative, sha in manifest["sha256"].items()
            if Path(relative).parts[-3] in selected}


def pending_central_coordinates(manifest):
    """Recheck immutable originals and skip complete, byte-identical resumed uploads."""
    verify_remote(CENTRAL_REPOSITORY, manifest["existingSha256"], {})
    pending = []
    specs = publication_specs()
    for artifact in manifest["uploadCoordinates"]:
        prefix = f"{GROUP_PATH}/{artifact}/{manifest['version']}/{artifact}-{manifest['version']}"
        probes = sorted({prefix + ".pom", prefix + primary_extension(specs[artifact])})
        present = [remote_matches(CENTRAL_REPOSITORY, relative, manifest["sha256"][relative], {})
                   for relative in probes]
        if any(present) and not all(present):
            raise ReleaseError(f"Central contains incomplete files for {artifact}; no upload or fallback attempted.")
        if all(present):
            verify_remote(CENTRAL_REPOSITORY, records_for_coordinates(manifest, [artifact]), {})
        else:
            pending.append(artifact)
    return pending


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
    multipart = output / "central-upload.multipart"
    with multipart.open("wb") as body:
        body.write((f"--{boundary}\r\nContent-Disposition: form-data; name=\"bundle\"; "
                    f"filename=\"central-bundle.zip\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode())
        with bundle.open("rb") as source:
            shutil.copyfileobj(source, body)
        body.write(f"\r\n--{boundary}--\r\n".encode())
    headers = {"Authorization": authorization, "Content-Type": "multipart/form-data; boundary=" + boundary,
               "Content-Length": str(multipart.stat().st_size)}
    try:
        with multipart.open("rb") as body:
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
    finally:
        multipart.unlink(missing_ok=True)
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
    pending = pending_central_coordinates(manifest)
    if not pending:
        verify_remote(CENTRAL_REPOSITORY, manifest["sha256"], {})
        record_success(output, manifest, "Maven Central", CENTRAL_REPOSITORY, already_published=True)
        return
    username = os.environ.get("ORG_GRADLE_PROJECT_mavenCentralUsername", "")
    password = os.environ.get("ORG_GRADLE_PROJECT_mavenCentralPassword", "")
    if not username.strip() or not password.strip():
        raise ReleaseError("Central credentials are missing; no Central request or Packages fallback was attempted.")
    pending_manifest = {**manifest, "sha256": records_for_coordinates(manifest, pending)}
    write_json(output / "upload-plan.json", {"coordinates": pending, "sha256": pending_manifest["sha256"],
               "retainedCentralCoordinates": [name for name in manifest["coordinates"] if name not in pending]})
    sign_payload(output, pending_manifest)
    records = all_publication_files(output, pending_manifest)
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
    retained = records_for_coordinates(manifest, [name for name in manifest["coordinates"] if name not in pending])
    verify_remote(CENTRAL_REPOSITORY, retained, {})
    if backend == "Maven Central":
        verify_remote(CENTRAL_REPOSITORY, manifest["sha256"], {}, wait_seconds=600)
    record_success(output, manifest, backend, repository_url, fallback_reason, uploaded_coordinates=pending)


def record_success(output, manifest, backend, repository_url, fallback_reason=None, already_published=False,
                   uploaded_coordinates=None):
    uploaded = uploaded_coordinates or []
    retained = [name for name in manifest["coordinates"] if name not in uploaded]
    result = {"version": manifest["version"], "backend": backend, "repository": repository_url,
              "nativePlatforms": manifest["nativePlatforms"], "coordinates": manifest["coordinates"],
              "verified": True, "fallbackReason": fallback_reason, "alreadyPublished": already_published,
              "uploadedCoordinates": uploaded, "uploadedCount": len(uploaded),
              "retainedCentralCoordinates": retained, "retainedCentralCount": len(retained),
              "resumedCoordinates": [name for name in retained if name not in manifest["existingCoordinates"]]}
    write_json(output / "result.json", result)
    message = (f"Verified the complete {len(manifest['coordinates'])}-coordinate release. "
               f"Uploaded {len(uploaded)} coordinates to {backend}; retained {len(retained)} unchanged on Maven Central. "
               f"Native platforms: {', '.join(manifest['nativePlatforms'])}. "
               "Includes Android, iOS, Wasm, JVM, both runtime aggregators and the FFmpeg XCFramework. "
               "Native publication coverage does not imply TAO window support on Linux.")
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
    parser.add_argument("--native-platform", action="append", choices=SUPPORTED_NATIVE)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "darriousliu/mediamp"))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--supplement-existing", action="store_true",
                        help="Preserve exact Central files and upload only coordinates not already published.")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+-tao(?:[.-][A-Za-z0-9.-]+)?", args.version):
        parser.error("Only explicit -tao release versions are accepted.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Expected a GitHub owner/repository name.")
    if args.native_platform is not None and len(set(args.native_platform)) != len(args.native_platform):
        parser.error("Duplicate native platforms are not allowed.")
    if args.prepare_only:
        if args.staging is None:
            parser.error("--prepare-only requires --staging.")
        freeze(args.staging, args.output, args.version, args.native_platform, args.supplement_existing)
    else:
        manifest = checked_manifest(args.output, args.version, args.native_platform, args.supplement_existing)
        publish(args.output, manifest, args.repository)


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, HTTPError, URLError, OSError, ValueError, KeyError) as error:
        print("Release failed: " + safe_message(str(error)), file=sys.stderr)
        sys.exit(1)
