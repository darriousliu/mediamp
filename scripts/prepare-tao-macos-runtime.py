#!/usr/bin/env python3
"""Rebuild this checkout's JNI bridge over the released macOS codec runtime.

This local development path avoids rebuilding unchanged mpv/FFmpeg dependencies.
The output is consumed by the existing prebuilt-runtime publication path when
Gradle is invoked with -Pmediamp.mpv.buildvariant=.
"""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import zipfile


BASE_VERSION = "0.4.0"
BASE_SHA256 = "71526df18b3dcbc5834cfe572c296649b49128a3ecbde8193912c6e90a7f708a"
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-runtime-jar", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("This local runtime recipe is for macOS arm64 only.")
    base = args.base_runtime_jar.resolve()
    if hashlib.sha256(base.read_bytes()).hexdigest() != BASE_SHA256:
        parser.error(f"Expected the official macOS arm64 runtime {BASE_VERSION} SHA-256.")
    version = next(
        line.partition("=")[2].strip()
        for line in (ROOT / "gradle.properties").read_text().splitlines()
        if line.startswith("version.name=")
    )
    if not version.endswith("-tao"):
        parser.error("version.name must end in -tao to avoid replacing a released runtime.")
    java_home = os.environ.get("JAVA_HOME") or subprocess.check_output(
        ["/usr/libexec/java_home"], text=True
    ).strip()
    source_dir = ROOT / "mediamp-mpv/src/cpp"
    sources = sorted(source_dir.glob("*.cpp")) + sorted(source_dir.glob("*.mm"))
    output_dir = ROOT / "mediamp-mpv/build/prebuilt-runtime-jars/macos-arm64"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"mediamp-mpv-runtime-{version}-macos-arm64.jar"
    with tempfile.TemporaryDirectory(prefix="mediamp-tao-native-") as tmp:
        runtime_dir = Path(tmp)
        with zipfile.ZipFile(base) as archive:
            for entry in archive.infolist():
                relative = Path(entry.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"Invalid archive path: {entry.filename}")
                if not entry.is_dir():
                    target = runtime_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(entry))
        library = runtime_dir / "libmediampv.dylib"
        subprocess.run(
            [
                "clang++", "-std=c++17", "-fPIC", "-fobjc-arc", "-O2", "-dynamiclib",
                "-mmacosx-version-min=12.0",
                "-I", str(source_dir / "include"),
                "-I", str(Path(java_home) / "include"),
                "-I", str(Path(java_home) / "include/darwin"),
                "-I", str(ROOT / "mediamp-mpv/mpv/include"),
                "-I", str(ROOT / "mediamp-ffmpeg/ffmpeg"),
                "-L", str(runtime_dir), "-lmpv", "-lavcodec",
                "-framework", "Foundation", "-framework", "Metal",
                "-framework", "IOSurface", "-framework", "OpenGL",
                "-framework", "QuartzCore", "-framework", "CoreGraphics",
                "-framework", "ImageIO",
                "-Wl,-install_name,@loader_path/libmediampv.dylib",
                "-Wl,-rpath,@loader_path",
                *(str(path) for path in sources), "-o", str(library),
            ],
            check=True,
        )
        subprocess.run(["codesign", "--force", "--sign", "-", str(library)], check=True)
        source_hash = hashlib.sha256()
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                source_hash.update(str(path.relative_to(source_dir)).encode())
                source_hash.update(path.read_bytes())
        provenance = runtime_dir / "META-INF/mediamp-tao-native-build.txt"
        provenance.parent.mkdir(exist_ok=True)
        provenance.write_text(
            f"version={version}\nbase-runtime={BASE_VERSION}\n"
            f"base-sha256={BASE_SHA256}\n"
            f"jni-source-sha256={source_hash.hexdigest()}\n"
            "codecs=upstream prebuilt; JNI=locally compiled from this checkout\n"
        )
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(runtime_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(runtime_dir).as_posix())
    print(output)


if __name__ == "__main__":
    main()
