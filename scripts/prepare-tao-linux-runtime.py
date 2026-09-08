#!/usr/bin/env python3
"""Rebuild Linux x64 JNI over verified upstream codecs; preserves AWT/GLX only.

Run on ubuntu-24.04 with g++, binutils, libgl1-mesa-dev, libx11-dev,
zlib1g-dev and a JDK (JAVA_HOME). No TAO EGL backend is implemented on Linux.
All runtime ELF files must stay within the upstream glibc 2.39 baseline.
"""

import argparse
import os
from pathlib import Path
import platform
import re
import tempfile

from tao_runtime import (ROOT, extract_base, fork_version, package_runtime,
                         posix_load_probe, mpv_jni_exports, run, sha256, shared_libraries,
                         tree_hash, verify_runtime_manifest, verify_mpv_codec_inputs, write_provenance)


def verify_glibc(runtime):
    requirements = set()
    for library in shared_libraries(runtime, "linux-x64"):
        output = run(["readelf", "--version-info", library], capture_output=True, text=True).stdout
        for name in re.findall(r"\bName:\s+(GLIBC_\S+)", output):
            if name == "GLIBC_ABI_DT_RELR":
                value = (2, 36)
            elif re.fullmatch(r"GLIBC_\d+(?:\.\d+)+", name):
                value = tuple(int(part) for part in name[6:].split("."))
            else:
                raise RuntimeError(f"Unknown glibc requirement {name} in {library.name}")
            if value > (2, 39):
                raise RuntimeError(f"{library.name} requires {name}; maximum baseline is GLIBC_2.39")
            requirements.add(name)
    return ",".join(sorted(requirements))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-runtime-jar", type=Path, required=True)
    args = parser.parse_args()
    verify_mpv_codec_inputs()
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        parser.error("Requires native Linux x64, preferably ubuntu-24.04.")
    if not os.environ.get("JAVA_HOME"):
        parser.error("Set JAVA_HOME to the Linux JDK with JNI/JAWT headers.")
    java_home = Path(os.environ["JAVA_HOME"])
    target, version = "linux-x64", fork_version()
    source_dir = ROOT / "mediamp-mpv/src/cpp"
    sources = sorted(source_dir.rglob("*.cpp"))
    includes = [source_dir / "include", java_home / "include", java_home / "include/linux",
                ROOT / "mediamp-mpv/mpv/include", ROOT / "mediamp-ffmpeg/ffmpeg"]
    for header in (includes[1] / "jni.h", includes[1] / "jawt.h", includes[2] / "jawt_md.h",
                   includes[3] / "mpv/client.h", includes[4] / "libavcodec/jni.h"):
        if not header.is_file():
            parser.error(f"Missing JNI/codec header: {header}")
    compiler_version = run(["g++", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    with tempfile.TemporaryDirectory(prefix="mediamp-tao-linux-x64-") as tmp:
        scratch = Path(tmp)
        runtime = scratch / "runtime"
        runtime.mkdir()
        extract_base(args.base_runtime_jar.resolve(), runtime, "mpv", target)
        verify_runtime_manifest(runtime, "mpv", target)
        library = runtime / "libmediampv.so"
        old_hash = sha256(library)
        library.unlink()
        run(["g++", "-pthread", "-std=c++17", "-O2", "-fPIC", "-shared",
             *(arg for directory in includes for arg in ("-I", directory)), *sources,
             "-L", runtime, "-lmpv", "-lavcodec", "-lGL", "-lX11", "-ldl", "-lz",
             "-Wl,-rpath,$ORIGIN", "-Wl,--no-undefined", "-o", library])
        if sha256(library) == old_hash:
            raise RuntimeError("Refusing unchanged upstream JNI output.")
        verify_runtime_manifest(runtime, "mpv", target)
        glibc_requirements = verify_glibc(runtime)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("LD_", "DYLD_"))}
        env["LD_LIBRARY_PATH"] = str(runtime)
        for binary in shared_libraries(runtime, target):
            dependencies = run(["ldd", binary], env=env, capture_output=True, text=True).stdout
            if "not found" in dependencies:
                raise RuntimeError(f"Unresolved packaged dependency: {binary.name}\n{dependencies}")
            for resolved in re.findall(r"=>\s+(/\S+)", dependencies):
                if not (resolved.startswith(str(runtime) + "/")
                        or resolved.startswith(("/lib/", "/lib64/", "/usr/lib/", "/usr/lib64/"))):
                    raise RuntimeError(f"Dependency resolved outside package/system libraries: {resolved}")
        methods = mpv_jni_exports("g++", ["-std=c++17"], includes)
        posix_load_probe(scratch, runtime, "g++", ["-std=c++17", "-ldl"], library.name, "libmpv.so", methods)
        write_provenance(runtime, "mpv", target, version, **{
            "jni": "compiled-from-checkout", "jni-source-sha256": tree_hash(source_dir),
            "jni-binary-sha256": sha256(library), "compiler": compiler_version,
            "codecs": "upstream prebuilt unchanged", "tao": "unsupported; AWT GLX backend only",
            "glibc-baseline": "2.39", "glibc-requirements": glibc_requirements,
            "verification": "native architecture; glibc versions; ldd closure; all JNI exports; headless mpv initialization",
            "gpu-playback": "not verified by this build script",
        })
        package_runtime(runtime, "mpv", target, version)


if __name__ == "__main__":
    main()
