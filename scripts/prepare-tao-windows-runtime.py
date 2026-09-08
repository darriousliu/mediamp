#!/usr/bin/env python3
"""Rebuild Windows x64 TAO JNI over the verified official mpv/FFmpeg runtime.

On a windows-2022 runner, set JAVA_HOME to a Windows x64 JDK and install MSYS2
UCRT64 packages mingw-w64-ucrt-x86_64-gcc and mingw-w64-ucrt-x86_64-python.
Run using native Windows Python (the UCRT64 Python also works), for example:

    python scripts/prepare-tao-windows-runtime.py --msys2-dir C:/msys64 \
        --base-runtime-jar path/to/mediamp-mpv-runtime-windows-x64-0.4.0.jar

Git and the mpv/FFmpeg submodules are required. Public mpv headers are exported
from the submodule's committed HEAD, then the repository's render_d3d11.patch is
applied to that temporary copy only. The checkout itself is never patched. The
existing prebuilt-runtime Gradle publication consumes the result when invoked
with -Pmediamp.mpv.buildvariant= -Pmediamp.ffmpeg.buildvariant=.

GNU ld supports linking directly to DLLs, so no import libraries or codec
rebuilds are needed: https://sourceware.org/binutils/docs/ld/WIN32.html . Only
the JNI wrapper changes. Its C++ runtime is linked statically to avoid requiring
new libstdc++ symbols from the older runtime bundled with the official codecs.
"""

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import tempfile
import zipfile


BASE_VERSION = "0.4.0"
BASE_SHA256 = "abe3b9fc6553a252d3d92f3e62494fe59adca46ba79f898c8f7ac298561ee0b3"
ROOT = Path(__file__).resolve().parents[1]
TAO_METHODS = (
    "nCreateRenderContextTaoD3D11",
    "nSetSurfaceConfigTaoD3D11",
    "nGetSharedTextureTaoD3D11",
    "nGetRetiredGenerationTaoD3D11",
    "nAckRetiredTaoD3D11Texture",
)
COMPILER_RUNTIME_DLLS = {"libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll"}

# This subprocess is built with static compiler runtimes. It cannot accidentally
# satisfy a missing packaged DLL using Python's or MSYS2's already loaded DLLs.
# LoadLibraryEx limits dependency lookup to the artifact directory and System32.
# It validates loading and the mpv C API, not GPU playback or JNI_OnLoad.
LOAD_PROBE = r'''
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <cstdio>
#include <mpv/client.h>

int wmain(int argc, wchar_t **argv) {
    if (argc != 2 || !SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)) return 1;
    HMODULE jni = LoadLibraryExW(argv[1], nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!jni) {
        std::fprintf(stderr, "Packaged mediampv.dll LoadLibraryEx failed: %lu\n", GetLastError());
        return 2;
    }
    const char *methods[] = { @TAO_METHODS@ };
    for (const char *method : methods) {
        if (!GetProcAddress(jni, method)) {
            std::fprintf(stderr, "Missing TAO JNI export: %s\n", method);
            return 3;
        }
    }
    HMODULE mpv = GetModuleHandleW(L"libmpv-2.dll");
    if (!mpv) return 4;
    auto api = reinterpret_cast<decltype(&mpv_client_api_version)>(GetProcAddress(mpv, "mpv_client_api_version"));
    auto create = reinterpret_cast<decltype(&mpv_create)>(GetProcAddress(mpv, "mpv_create"));
    auto option = reinterpret_cast<decltype(&mpv_set_option_string)>(GetProcAddress(mpv, "mpv_set_option_string"));
    auto initialize = reinterpret_cast<decltype(&mpv_initialize)>(GetProcAddress(mpv, "mpv_initialize"));
    auto destroy = reinterpret_cast<decltype(&mpv_terminate_destroy)>(GetProcAddress(mpv, "mpv_terminate_destroy"));
    if (!api || !create || !option || !initialize || !destroy) return 5;
    if ((api() >> 16) != (MPV_CLIENT_API_VERSION >> 16) || api() < MPV_CLIENT_API_VERSION) {
        std::fprintf(stderr, "mpv API/header mismatch: runtime=%lu headers=%lu\n", api(), MPV_CLIENT_API_VERSION);
        return 8;
    }
    mpv_handle *handle = create();
    if (!handle) return 6;
    int rc = option(handle, "config", "no");
    if (rc >= 0) rc = option(handle, "vo", "null");
    if (rc >= 0) rc = option(handle, "ao", "null");
    if (rc >= 0) rc = initialize(handle);
    destroy(handle);
    if (rc < 0) {
        std::fprintf(stderr, "Packaged mpv initialization failed: %d\n", rc);
        return 7;
    }
    std::printf("Verified 5 TAO JNI exports; mpv initialized; client API=%lu\n", api());
    FreeLibrary(jni);
    return 0;
}
'''


def extract_base(base, destination):
    """Verify before extracting, and keep every codec byte unchanged."""
    if hashlib.sha256(base.read_bytes()).hexdigest() != BASE_SHA256:
        raise ValueError(f"Expected official Windows x64 runtime {BASE_VERSION} SHA-256 {BASE_SHA256}")
    with zipfile.ZipFile(base) as archive:
        for entry in archive.infolist():
            relative = PurePosixPath(entry.filename)
            if (relative.is_absolute() or ".." in relative.parts
                    or "\\" in entry.filename or ":" in entry.filename):
                raise ValueError(f"Invalid archive path: {entry.filename}")
            if not entry.is_dir():
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))


def run(command, env, **kwargs):
    return subprocess.run([str(arg) for arg in command], env=env, check=True, **kwargs)


def prepare_mpv_headers(mpv_source, patch, destination, env):
    """Apply only public-header hunks to an isolated export, including render.h.

    A fresh stock mpv submodule has neither render_d3d11.h nor its render.h enum
    entries. Exporting committed headers also works when a previous Gradle build
    already patched the user's submodule working tree, without applying twice.
    """
    tracked = run(["git", "-C", mpv_source, "ls-tree", "-r", "--name-only",
                   "HEAD", "include/mpv"], env, capture_output=True, text=True).stdout.splitlines()
    for name in tracked:
        relative = PurePosixPath(name)
        if relative.parts[:2] != ("include", "mpv") or ".." in relative.parts:
            raise ValueError(f"Invalid public header path: {name}")
        if relative.suffix != ".h":
            continue
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(run(["git", "-C", mpv_source, "show", f"HEAD:{name}"],
                               env, capture_output=True).stdout)
    run(["git", "apply", "--no-index", "--include=include/mpv/*.h", "--check", patch],
        env, cwd=destination)
    run(["git", "apply", "--no-index", "--include=include/mpv/*.h", patch], env, cwd=destination)
    include_dir = destination / "include"
    for name in ("client.h", "render.h", "render_gl.h", "stream_cb.h", "render_d3d11.h"):
        if not (include_dir / "mpv" / name).is_file():
            raise RuntimeError(f"Patched public mpv header is missing: {name}")
    return include_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-runtime-jar", type=Path, required=True)
    parser.add_argument("--msys2-dir", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("This recipe requires native Windows x64 Python and the MSYS2 UCRT64 toolchain.")
    java_home = os.environ.get("JAVA_HOME")
    if not java_home:
        parser.error("Set JAVA_HOME to a Windows x64 JDK (JNI headers are required).")
    source_dir = ROOT / "mediamp-mpv/src/cpp"
    include_dirs = [source_dir / "include", Path(java_home) / "include",
                    Path(java_home) / "include/win32", ROOT / "mediamp-mpv/mpv/include",
                    ROOT / "mediamp-ffmpeg/ffmpeg"]
    required_headers = [include_dirs[1] / "jni.h", include_dirs[2] / "jni_md.h",
                        include_dirs[3] / "mpv/client.h", include_dirs[4] / "libavcodec/jni.h"]
    for header in required_headers:
        if not header.is_file():
            parser.error(f"Missing header {header}; initialize submodules and configure JAVA_HOME.")
    compiler_bin = args.msys2_dir.resolve() / "ucrt64/bin"
    compiler, objdump = compiler_bin / "g++.exe", compiler_bin / "objdump.exe"
    for tool in (compiler, objdump):
        if not tool.is_file():
            parser.error(f"Missing {tool}; install mingw-w64-ucrt-x86_64-gcc and binutils.")
    env = dict(os.environ, PATH=str(compiler_bin) + os.pathsep + os.environ.get("PATH", ""))
    env["LC_ALL"] = "C"
    target = run([compiler, "-dumpmachine"], env, capture_output=True, text=True).stdout.strip()
    if target != "x86_64-w64-mingw32":
        parser.error(f"Expected x86_64-w64-mingw32 compiler, got {target}.")
    compiler_version = run([compiler, "--version"], env, capture_output=True, text=True).stdout.splitlines()[0]
    version = next(line.partition("=")[2].strip()
                   for line in (ROOT / "gradle.properties").read_text().splitlines()
                   if line.startswith("version.name="))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*-tao", version):
        parser.error("version.name must end in -tao to avoid replacing a released runtime.")
    sources = sorted(source_dir.rglob("*.cpp"))
    if not sources:
        parser.error(f"No JNI C++ sources found in {source_dir}.")
    output_dir = ROOT / "mediamp-mpv/build/prebuilt-runtime-jars/windows-x64"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"mediamp-mpv-runtime-{version}-windows-x64.jar"
    with tempfile.TemporaryDirectory(prefix="mediamp-tao-windows-") as tmp:
        scratch = Path(tmp)
        header_patch = ROOT / "mediamp-mpv/render_d3d11.patch"
        include_dirs[3] = prepare_mpv_headers(ROOT / "mediamp-mpv/mpv", header_patch,
                                             scratch / "mpv-headers", env)
        runtime_dir = scratch / "runtime"
        runtime_dir.mkdir()
        extract_base(args.base_runtime_jar.resolve(), runtime_dir)
        library = runtime_dir / "mediampv.dll"
        old_jni_hash = hashlib.sha256(library.read_bytes()).hexdigest()
        library.unlink()  # A compiler failure must never leave the upstream JNI as the output.
        includes = [arg for directory in include_dirs for arg in ("-I", directory)]
        compile_flags = ["-std=c++17", "-O2", "-D_WIN32_WINNT=0x0A00",
                         "-static-libgcc", "-static-libstdc++"]
        # Put static libstdc++ before winpthread so the latter resolves its thread
        # references. Keep system DLL imports dynamic after this group.
        static_runtime = ["-Wl,-Bstatic", "-lstdc++", "-lwinpthread", "-Wl,-Bdynamic"]
        run([compiler, *compile_flags, "-shared", *includes, *sources,
             runtime_dir / "libmpv-2.dll", runtime_dir / "avcodec-62.dll",
             *static_runtime, "-ld3d11", "-ld3d12", "-ldxgi", "-ldxguid",
             "-lwindowscodecs", "-lole32", "-lopengl32", "-lgdi32",
             "-Wl,--no-undefined", "-o", library], env)
        if hashlib.sha256(library.read_bytes()).hexdigest() == old_jni_hash:
            raise RuntimeError("Rebuilt JNI is identical to upstream; refusing to label it TAO.")
        binary_info = run([objdump, "-p", library], env, capture_output=True, text=True).stdout
        dependencies = set(re.findall(r"DLL Name:\s*(\S+)", binary_info))
        if not dependencies or "libmpv-2.dll" not in {name.lower() for name in dependencies}:
            raise RuntimeError("Could not verify the rebuilt JNI's libmpv dependency.")
        unexpected = COMPILER_RUNTIME_DLLS & {name.lower() for name in dependencies}
        if unexpected:
            raise RuntimeError(f"JNI still imports compiler runtime DLLs: {sorted(unexpected)}")
        methods = [f"Java_org_openani_mediamp_mpv_MPVHandleDesktop_{method}" for method in TAO_METHODS]
        probe_source = scratch / "load-probe.cpp"
        probe_source.write_text(LOAD_PROBE.replace("@TAO_METHODS@", ", ".join(f'"{method}"' for method in methods)))
        probe = scratch / "load-probe.exe"
        run([compiler, *compile_flags, "-municode", *includes, probe_source,
             *static_runtime, "-o", probe], env)
        # Hide the build tools from the probe to catch missing packaged dependencies.
        probe_env = dict(env, PATH=str(Path(os.environ["SystemRoot"]) / "System32"))
        run([probe, library], probe_env, cwd=scratch)
        source_hash = hashlib.sha256()
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                source_hash.update(path.relative_to(source_dir).as_posix().encode())
                source_hash.update(path.read_bytes())
        provenance = runtime_dir / "META-INF/mediamp-tao-native-build.txt"
        provenance.parent.mkdir(exist_ok=True)
        provenance.write_text(
            f"version={version}\nbase-runtime={BASE_VERSION}\nbase-sha256={BASE_SHA256}\n"
            f"jni-source-sha256={source_hash.hexdigest()}\ncompiler={compiler_version}\n"
            f"mpv-header-patch-sha256={hashlib.sha256(header_patch.read_bytes()).hexdigest()}\n"
            "compiler-runtime=static GCC/libstdc++/winpthread\n"
            "codecs=upstream prebuilt; JNI=compiled from this checkout\n"
            "verification=restricted DLL loading; 5 TAO JNI exports; headless mpv initialization\n"
            "gpu-playback=not verified by this build script\n"
        )
        staged = scratch / output.name
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(runtime_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(runtime_dir).as_posix())
        # Copy only after every build/load check succeeds; never emit a partial JAR.
        pending = output.with_suffix(".jar.tmp")
        pending.write_bytes(staged.read_bytes())
        pending.replace(output)
    print(output)


if __name__ == "__main__":
    main()
