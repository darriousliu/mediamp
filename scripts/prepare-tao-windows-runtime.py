#!/usr/bin/env python3
"""Rebuild Windows x64/arm64 TAO JNI over the verified official mpv/FFmpeg runtime.

On a windows-2022 runner, set JAVA_HOME to a Windows x64 JDK and install MSYS2
UCRT64 packages mingw-w64-ucrt-x86_64-gcc and mingw-w64-ucrt-x86_64-python.
Run using native Windows Python (the UCRT64 Python also works), for example:

    python scripts/prepare-tao-windows-runtime.py --msys2-dir C:/msys64 \
        --base-runtime-jar path/to/mediamp-mpv-runtime-windows-x64-0.4.0.jar

For native Windows arm64 use windows-11-arm, ARM64 Python/JDK, MSYS2
CLANGARM64 packages mingw-w64-clang-aarch64-clang, lld, llvm, libc++ and libunwind.
ARM64 imports are generated from the exact runtime DLL exports with llvm-dlltool.

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


from tao_runtime import (ROOT, extract_base as extract_verified_base, fork_version,
                         package_runtime, mpv_jni_exports, sha256, tree_hash,
                         verify_binary_arch, verify_runtime_manifest, verify_mpv_codec_inputs, write_provenance)
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
    std::printf("Verified all JNI exports; mpv initialized; client API=%lu\n", api());
    FreeLibrary(jni);
    return 0;
}
'''


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


def make_arm64_import_library(dll, compiler_bin, scratch, env):
    # lld does not share GNU ld's direct-DLL-input contract. Generate import
    # libraries from the exact verified runtime's exports instead.
    output = run([compiler_bin / "llvm-readobj.exe", "--coff-exports", dll], env,
                 capture_output=True, text=True).stdout
    names = re.findall(r"^\s+Name: (\S+)\s*$", output, re.MULTILINE)
    prefix = "mpv_" if dll.name == "libmpv-2.dll" else "av_jni_"
    names = sorted(name for name in names if name.startswith(prefix))
    if not names:
        raise RuntimeError(f"No required exports in {dll.name}")
    definition = scratch / (dll.name + ".def")
    definition.write_text("LIBRARY " + dll.name + "\nEXPORTS\n" + "\n".join(names) + "\n")
    import_lib = scratch / (dll.name + ".a")
    run([compiler_bin / "llvm-dlltool.exe", "-m", "arm64", "-d", definition,
         "-l", import_lib, "-D", dll.name], env)
    return import_lib


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-runtime-jar", type=Path, required=True)
    parser.add_argument("--msys2-dir", type=Path, required=True)
    args = parser.parse_args()
    verify_mpv_codec_inputs()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if platform.system() != "Windows" or arch is None:
        parser.error("Requires native Windows x64/arm64 Python and matching MSYS2 toolchain.")
    if not os.environ.get("JAVA_HOME"):
        parser.error("Set JAVA_HOME to a matching Windows JDK.")
    runtime_target = f"windows-{arch}"
    source_dir = ROOT / "mediamp-mpv/src/cpp"
    java_home = Path(os.environ["JAVA_HOME"])
    include_dirs = [source_dir / "include", java_home / "include", java_home / "include/win32",
                    ROOT / "mediamp-mpv/mpv/include", ROOT / "mediamp-ffmpeg/ffmpeg"]
    for header in (include_dirs[1] / "jni.h", include_dirs[2] / "jni_md.h",
                   include_dirs[3] / "mpv/client.h", include_dirs[4] / "libavcodec/jni.h"):
        if not header.is_file():
            parser.error(f"Missing header {header}; initialize submodules and JAVA_HOME.")
    compiler_bin = args.msys2_dir.resolve() / ("ucrt64/bin" if arch == "x64" else "clangarm64/bin")
    compiler = compiler_bin / ("g++.exe" if arch == "x64" else "clang++.exe")
    objdump = compiler_bin / ("objdump.exe" if arch == "x64" else "llvm-objdump.exe")
    required_tools = [compiler, objdump]
    if arch == "arm64":
        required_tools += [compiler_bin / "llvm-readobj.exe", compiler_bin / "llvm-dlltool.exe"]
    for tool in required_tools:
        if not tool.is_file():
            parser.error(f"Missing tool: {tool}")
    env = dict(os.environ, PATH=str(compiler_bin) + os.pathsep + os.environ.get("PATH", ""), LC_ALL="C")
    compiler_target = run([compiler, "-dumpmachine"], env, capture_output=True, text=True).stdout.strip()
    expected = "x86_64-w64-mingw32" if arch == "x64" else "aarch64-w64-windows-gnu"
    accepted_targets = {expected} if arch == "x64" else {expected, "aarch64-w64-mingw32"}
    if compiler_target not in accepted_targets:
        parser.error(f"Unexpected compiler target {compiler_target}; expected {sorted(accepted_targets)}")
    compiler_version = run([compiler, "--version"], env, capture_output=True, text=True).stdout.splitlines()[0]
    version = fork_version()
    sources = sorted(source_dir.rglob("*.cpp"))
    if not sources:
        parser.error(f"No JNI C++ sources in {source_dir}")
    with tempfile.TemporaryDirectory(prefix=f"mediamp-tao-{runtime_target}-") as tmp:
        scratch = Path(tmp)
        header_patch = ROOT / "mediamp-mpv/render_d3d11.patch"
        include_dirs[3] = prepare_mpv_headers(ROOT / "mediamp-mpv/mpv", header_patch,
                                             scratch / "mpv-headers", env)
        runtime_dir = scratch / "runtime"
        runtime_dir.mkdir()
        extract_verified_base(args.base_runtime_jar.resolve(), runtime_dir, "mpv", runtime_target)
        verify_runtime_manifest(runtime_dir, "mpv", runtime_target)
        library = runtime_dir / "mediampv.dll"
        old_jni_hash = sha256(library)
        library.unlink()
        includes = [arg for directory in include_dirs for arg in ("-I", directory)]
        compile_flags = ["-std=c++17", "-O2", "-D_WIN32_WINNT=0x0A00"]
        if arch == "x64":
            compile_flags += ["-static-libgcc", "-static-libstdc++"]
            static_runtime = ["-Wl,-Bstatic", "-lstdc++", "-lwinpthread", "-Wl,-Bdynamic"]
            codec_links = [runtime_dir / "libmpv-2.dll", runtime_dir / "avcodec-62.dll"]
            runtime_description = "static GCC/libstdc++/winpthread"
        else:
            # CLANGARM64 uses libc++ and compiler-rt, never GCC/libstdc++.
            compile_flags += ["-static", "-stdlib=libc++", "-fuse-ld=lld"]
            static_runtime = []
            codec_links = [make_arm64_import_library(runtime_dir / name, compiler_bin, scratch, env)
                           for name in ("libmpv-2.dll", "avcodec-62.dll")]
            runtime_description = "static Clang compiler-rt/libc++/libunwind"
        run([compiler, *compile_flags, "-shared", *includes, *sources, *codec_links,
             *static_runtime, "-ld3d11", "-ld3d12", "-ldxgi", "-ldxguid",
             "-lwindowscodecs", "-lole32", "-lopengl32", "-lgdi32",
             "-Wl,--no-undefined", "-o", library], env)
        if sha256(library) == old_jni_hash:
            raise RuntimeError("Refusing unchanged upstream JNI output.")
        verify_runtime_manifest(runtime_dir, "mpv", runtime_target)
        binary_info = run([objdump, "-p", library], env, capture_output=True, text=True).stdout
        dependencies = {name.lower() for name in re.findall(r"DLL Name:\s*(\S+)", binary_info)}
        if "libmpv-2.dll" not in dependencies:
            raise RuntimeError("Could not verify rebuilt JNI libmpv dependency.")
        forbidden = COMPILER_RUNTIME_DLLS | {"libc++.dll", "libunwind.dll", "libc++abi.dll"}
        if dependencies & forbidden:
            raise RuntimeError(f"JNI still imports compiler runtime DLLs: {sorted(dependencies & forbidden)}")
        methods = mpv_jni_exports(compiler, compile_flags, include_dirs, env)
        probe_source = scratch / "load-probe.cpp"
        probe_source.write_text(LOAD_PROBE.replace("@TAO_METHODS@", ", ".join(f'"{m}"' for m in methods)))
        probe = scratch / "load-probe.exe"
        run([compiler, *compile_flags, "-municode", *includes, probe_source,
             *static_runtime, "-o", probe], env)
        verify_binary_arch(probe, runtime_target)
        probe_env = dict(env, PATH=str(Path(os.environ["SystemRoot"]) / "System32"))
        run([probe, library], probe_env, cwd=scratch)
        write_provenance(runtime_dir, "mpv", runtime_target, version, **{
            "jni": "compiled-from-checkout", "jni-source-sha256": tree_hash(source_dir),
            "jni-binary-sha256": sha256(library), "compiler": compiler_version,
            "mpv-header-patch-sha256": sha256(header_patch), "compiler-runtime": runtime_description,
            "codecs": "upstream prebuilt unchanged", "tao": "D3D11 shared texture",
            "verification": "native architecture; restricted DLL loading; all JNI exports; headless mpv initialization",
            "gpu-playback": "not verified by this build script",
        })
        package_runtime(runtime_dir, "mpv", runtime_target, version)


if __name__ == "__main__":
    main()
