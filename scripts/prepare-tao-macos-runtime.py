#!/usr/bin/env python3
"""Rebuild current macOS JNI over verified upstream 0.4.0 codecs on native arm64/x64.

Use macos-15 (arm64) or macos-15-intel (x64), Xcode clang and a matching JDK.
No Gradle/native codec build is required. This checks binary architecture, every
JNI export, relocatable dylib dependencies, and headless mpv initialization.
It does not claim that a GPU/window playback test ran.
"""

import argparse
import os
from pathlib import Path
import platform
import subprocess
import tempfile

from tao_runtime import (ROOT, extract_base, fork_version, package_runtime,
                         posix_load_probe, mpv_jni_exports, run, sha256, shared_libraries,
                         tree_hash, verify_runtime_manifest, verify_mpv_codec_inputs, write_provenance)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-runtime-jar", type=Path, required=True)
    args = parser.parse_args()
    verify_mpv_codec_inputs()
    arch = {"arm64": "arm64", "x86_64": "x64"}.get(platform.machine().lower())
    if platform.system() != "Darwin" or arch is None:
        parser.error("Requires native macOS arm64 or x64 Python and a matching JDK.")
    target = f"macos-{arch}"
    version = fork_version()
    java_home = Path(os.environ.get("JAVA_HOME") or subprocess.check_output(
        ["/usr/libexec/java_home"], text=True).strip())
    source_dir = ROOT / "mediamp-mpv/src/cpp"
    sources = sorted(source_dir.rglob("*.cpp")) + sorted(source_dir.rglob("*.mm"))
    includes = [source_dir / "include", java_home / "include", java_home / "include/darwin",
                ROOT / "mediamp-mpv/mpv/include", ROOT / "mediamp-ffmpeg/ffmpeg"]
    for header in (includes[1] / "jni.h", includes[2] / "jni_md.h",
                   includes[3] / "mpv/client.h", includes[4] / "libavcodec/jni.h"):
        if not header.is_file():
            parser.error(f"Missing JNI/codec header: {header}")
    flags = ["-arch", "arm64" if arch == "arm64" else "x86_64", "-mmacosx-version-min=12.0"]
    compiler_version = run(["clang++", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    with tempfile.TemporaryDirectory(prefix=f"mediamp-tao-{target}-") as tmp:
        scratch = Path(tmp)
        runtime = scratch / "runtime"
        runtime.mkdir()
        extract_base(args.base_runtime_jar.resolve(), runtime, "mpv", target)
        verify_runtime_manifest(runtime, "mpv", target)
        library = runtime / "libmediampv.dylib"
        old_hash = sha256(library)
        library.unlink()
        run(["clang++", *flags, "-std=c++17", "-fPIC", "-fobjc-arc", "-O2", "-dynamiclib",
             *(arg for directory in includes for arg in ("-I", directory)),
             *sources, "-L", runtime, "-lmpv", "-lavcodec",
             "-framework", "Foundation", "-framework", "Metal", "-framework", "IOSurface",
             "-framework", "OpenGL", "-framework", "QuartzCore", "-framework", "CoreGraphics",
             "-framework", "ImageIO", "-Wl,-install_name,@loader_path/libmediampv.dylib",
             "-Wl,-rpath,@loader_path", "-o", library])
        if sha256(library) == old_hash:
            raise RuntimeError("Refusing unchanged upstream JNI output.")
        run(["codesign", "--force", "--sign", "-", library])
        run(["codesign", "--verify", "--strict", library])
        verify_runtime_manifest(runtime, "mpv", target)
        for dylib in shared_libraries(runtime, target):
            dependencies = run(["otool", "-L", dylib], capture_output=True, text=True).stdout.splitlines()[1:]
            for line in dependencies:
                dependency = line.strip().split(" (", 1)[0]
                if dependency.startswith(("/usr/lib/", "/System/Library/")):
                    continue
                if dependency.startswith(("@rpath/", "@loader_path/")) and (runtime / Path(dependency).name).is_file():
                    continue
                raise RuntimeError(f"Non-relocatable/missing dylib dependency: {dylib.name}: {dependency}")
        methods = mpv_jni_exports("clang++", [*flags, "-std=c++17"], includes)
        posix_load_probe(scratch, runtime, "clang++", flags, library.name, "libmpv.dylib", methods)
        write_provenance(runtime, "mpv", target, version, **{
            "jni": "compiled-from-checkout", "jni-source-sha256": tree_hash(source_dir),
            "jni-binary-sha256": sha256(library), "compiler": compiler_version,
            "codecs": "upstream prebuilt unchanged", "tao": "Metal IOSurface",
            "verification": "native architecture; dylib closure; all JNI exports; headless mpv initialization",
            "gpu-playback": "not verified by this build script",
        })
        package_runtime(runtime, "mpv", target, version)


if __name__ == "__main__":
    main()
