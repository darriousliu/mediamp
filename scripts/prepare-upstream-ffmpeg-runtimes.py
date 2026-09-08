#!/usr/bin/env python3
"""Repackage five unchanged upstream FFmpeg desktop runtimes with fork provenance.

No arguments downloads checksum-pinned 0.4.0 JARs from Maven Central. An optional
--cache-dir reuses a download cache (every input is still SHA-256 checked).
This NEVER claims to recompile FFmpeg JNI: the JNI/wrapper/JVM API/codec refs must
match upstream. Android/iOS are built separately by the normal native pipeline.
"""

import argparse
from pathlib import Path
import tempfile
import urllib.request
import zipfile

from tao_runtime import (ROOT, BASE_VERSION, BASE_SHA256, FFMPEG_COMPATIBILITY_FILES, UPSTREAM_RELEASE_COMMIT,
                         extract_base, ffmpeg_compatibility_source_hash, fork_version,
                         package_runtime, sha256, tree_hash, verify_runtime_manifest,
                         write_provenance)


def verify_unchanged_payload(base, runtime):
    with zipfile.ZipFile(base) as archive:
        for name in archive.namelist():
            if not name.endswith("/") and archive.read(name) != (runtime / name).read_bytes():
                raise RuntimeError(f"Upstream FFmpeg payload changed: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "build/upstream-native-cache")
    args = parser.parse_args()
    compatibility = ffmpeg_compatibility_source_hash()
    version = fork_version()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    for target, expected in BASE_SHA256["ffmpeg"].items():
        artifact = f"mediamp-ffmpeg-runtime-{target}"
        filename = f"{artifact}-{BASE_VERSION}.jar"
        base = args.cache_dir / filename
        if not base.is_file():
            url = f"https://repo.maven.apache.org/maven2/org/openani/mediamp/{artifact}/{BASE_VERSION}/{filename}"
            pending = base.with_suffix(".jar.download")
            try:
                with urllib.request.urlopen(url, timeout=120) as response, pending.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                if sha256(pending) != expected:
                    raise ValueError(f"Upstream download checksum mismatch: {filename}")
                pending.replace(base)
            finally:
                pending.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"mediamp-ffmpeg-{target}-") as tmp:
            runtime = Path(tmp)
            extract_base(base, runtime, "ffmpeg", target)
            verify_runtime_manifest(runtime, "ffmpeg", target)
            jni_name = ("ffmpegkitjni.dll" if target.startswith("windows") else
                        "libffmpegkitjni.dylib" if target.startswith("macos") else "libffmpegkitjni.so")
            if not (runtime / jni_name).is_file():
                raise ValueError(f"Missing FFmpeg JNI wrapper: {jni_name}")
            # Reuse every codec/CLI/JNI/manifest byte; only the new provenance entry changes.
            verify_unchanged_payload(base, runtime)
            write_provenance(runtime, "ffmpeg", target, version, **{
                "jni": "reused-upstream-unchanged", "jni-binary-sha256": sha256(runtime / jni_name),
                "compatibility-source-sha256": compatibility,
                "compatibility-source-ref": UPSTREAM_RELEASE_COMMIT,
                "compatibility-release-tag": "v0.4.0",
                "compatibility-source-files": ",".join(sorted(FFMPEG_COMPATIBILITY_FILES)),
                "build-config-sha256": tree_hash(ROOT / "buildSrc/src/main/kotlin/ffmpeg"),
                "codecs": "upstream prebuilt unchanged",
                "verification": "pinned upstream JAR SHA256; architecture; manifest; every original entry byte-identical; current JNI ABI inputs match upstream",
                "native-load": "not rerun by this cross-platform repack script",
            })
            package_runtime(runtime, "ffmpeg", target, version)


if __name__ == "__main__":
    main()
