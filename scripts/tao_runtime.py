"""Shared, fail-closed preparation checks for the fork's desktop native JARs."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE_VERSION = "0.4.0"
BASE_SHA256 = {
    "mpv": {
        "macos-arm64": "71526df18b3dcbc5834cfe572c296649b49128a3ecbde8193912c6e90a7f708a",
        "macos-x64": "db6c639817ddabb90aeb7223e1fcf6aaa802f2e152b5e67ad811794fbfba3956",
        "windows-x64": "abe3b9fc6553a252d3d92f3e62494fe59adca46ba79f898c8f7ac298561ee0b3",
        "windows-arm64": "5199e470d859d8e68e41f891d5107024291035664e70ad7af621f2da45710758",
        "linux-x64": "884bf5a95dd156976c2d4fafe36dd339bab26bc984fd372d13243cf96bbe276c",
    },
    "ffmpeg": {
        "macos-arm64": "1f6cc2f864ed1eae4bf6ef84911357d9037f842be017dd1734b51dee55b97bc5",
        "macos-x64": "1316b36dff14e36779f93fde57a594bf1e9d3f3a8994c5002c0bd53536c88851",
        "windows-x64": "ce8eb06000b55c8b131ccb735e19110d6e40ed5feace7cc05dae7f5780eece3a",
        "windows-arm64": "5e5f7b4dce8cc16201a770c7ff3dfceac231c007156d20c6d8d07dd7c8f8e70b",
        "linux-x64": "15e974bf86a31685f282656f2672a992c0c69f08089bcda4f1947ed4f1ecc2fa",
    },
}

# Desktop native ABI inputs from official annotated tag v0.4.0 (tag object
# f7e59e998de1618a91ae5a6169dfc9576e535812), which peels to this commit.
# Build/publishing scripts may change without changing the reused binary ABI.
UPSTREAM_RELEASE_COMMIT = "970c114bc80c70610f241cc28e38fdc416b1ddd2"
FFMPEG_COMPATIBILITY_FILES = (
    "mediamp-ffmpeg/src/jvmMain/c/ffmpegkit_jni.c",
    "mediamp-ffmpeg/src/appleMain/c/ffmpegkit_wrapper.c",
    "mediamp-ffmpeg/src/appleMain/include/MediampFFmpegKit.h",
    "mediamp-ffmpeg/src/jvmMain/kotlin/org/openani/mediamp/ffmpeg/JvmFFmpegProcess.kt",
    "mediamp-ffmpeg/fix-prepare-cli-args-on-win-and-clean-up.patch",
)
FFMPEG_SUBMODULES = {
    "mediamp-ffmpeg/dav1d": "54706fc6bc0cdecab7e9593974a4039cc038fca7",
    "mediamp-ffmpeg/ffmpeg": "449453a9713aa1af31a12e148fb0caba80ed02d7",
}
FFMPEG_COMPATIBILITY_SHA256 = "4a3d7a65902202709b52e41ece2b6e5fb2bf4f122609376241f9d065a645d826"


def ffmpeg_compatibility_source_hash():
    """Hash sorted path + NUL + bytes + NUL, then sorted submodule path/ref pairs.

    Read the actual source files (including uncommitted changes), and require the
    codec submodule HEADs used by upstream. This deliberately refuses reuse after
    a JNI/wrapper/API/patch change. A codec rebuild is required in that case.
    """
    digest = hashlib.sha256()
    for relative in sorted(FFMPEG_COMPATIBILITY_FILES):
        # These are text ABI inputs; Git's Windows CRLF conversion is not a
        # source change and must not make the Mac publication verifier disagree.
        content = (ROOT / relative).read_bytes().replace(b"\r\n", b"\n")
        digest.update(relative.encode() + b"\0" + content + b"\0")
    for relative, expected in sorted(FFMPEG_SUBMODULES.items()):
        actual = run(["git", "-C", ROOT / relative, "rev-parse", "HEAD"],
                     capture_output=True, text=True).stdout.strip()
        if actual != expected:
            raise ValueError(f"FFmpeg reuse needs {relative} at {expected}, got {actual}")
        digest.update(relative.encode() + b"\0" + actual.encode() + b"\0")
    result = digest.hexdigest()
    if result != FFMPEG_COMPATIBILITY_SHA256:
        raise ValueError("FFmpeg desktop JNI/wrapper/API inputs differ from upstream 0.4.0; rebuild native FFmpeg.")
    return result


def run(command, env=None, **kwargs):
    return subprocess.run([str(arg) for arg in command], env=env, check=True, **kwargs)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fork_version():
    version = next(line.partition("=")[2].strip()
                   for line in (ROOT / "gradle.properties").read_text().splitlines()
                   if line.startswith("version.name="))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*-tao(?:\.[0-9]+)?", version):
        raise ValueError("version.name must end in -tao or -tao.<number>.")
    return version


def tree_hash(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def verify_mpv_codec_inputs():
    """The custom D3D11 render ABI is not covered by the stock mpv API version."""
    refs = {"mediamp-mpv/mpv": "41f6a645068483470267271e1d09966ca3b9f413",
            "mediamp-ffmpeg/ffmpeg": FFMPEG_SUBMODULES["mediamp-ffmpeg/ffmpeg"]}
    for relative, expected in refs.items():
        actual = run(["git", "-C", ROOT / relative, "rev-parse", "HEAD"],
                     capture_output=True, text=True).stdout.strip()
        if actual != expected:
            raise ValueError(f"Upstream 0.4.0 codec reuse requires {relative} at {expected}")
    patch = ROOT / "mediamp-mpv/render_d3d11.patch"
    # Normalize Git's CRLF conversion; the patch content itself must be exact.
    actual = hashlib.sha256(patch.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    if actual != "eb96209a832311794c781b9c52c1b733f7516bc0b1098128be8196b115752c48":
        raise ValueError("D3D11 render ABI patch differs from upstream 0.4.0; rebuild libmpv.")


def extract_base(base, destination, component, target):
    expected = BASE_SHA256[component][target]
    if sha256(base) != expected:
        raise ValueError(f"Expected official {component} {target} {BASE_VERSION} SHA-256 {expected}")
    with zipfile.ZipFile(base) as archive:
        seen = set()
        for entry in archive.infolist():
            relative = PurePosixPath(entry.filename)
            if (relative.is_absolute() or ".." in relative.parts
                    or "\\" in entry.filename or ":" in entry.filename
                    or entry.filename in seen):
                raise ValueError(f"Invalid or duplicate archive path: {entry.filename}")
            seen.add(entry.filename)
            if not entry.is_dir():
                path = destination.joinpath(*relative.parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(entry))


def verify_binary_arch(path, target):
    """Read the binary header, independent of the current host/toolchain."""
    data = path.read_bytes()[:4096]
    expected_arch = target.split("-")[1]
    if target.startswith("windows-"):
        if data[:2] != b"MZ":
            raise ValueError(f"Not a PE binary: {path}")
        pe = struct.unpack_from("<I", data, 0x3c)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError(f"Invalid PE header: {path}")
        actual = struct.unpack_from("<H", data, pe + 4)[0]
        expected = {"x64": 0x8664, "arm64": 0xaa64}[expected_arch]
    elif target.startswith("macos-"):
        if data[:4] != b"\xcf\xfa\xed\xfe":
            raise ValueError(f"Expected thin Mach-O 64-bit binary: {path}")
        actual = struct.unpack_from("<I", data, 4)[0]
        expected = {"x64": 0x1000007, "arm64": 0x100000c}[expected_arch]
    else:
        if data[:6] != b"\x7fELF\x02\x01":
            raise ValueError(f"Expected little-endian ELF64 binary: {path}")
        actual = struct.unpack_from("<H", data, 18)[0]
        expected = 62
    if actual != expected:
        raise ValueError(f"Wrong architecture for {target}: {path} ({actual:#x})")


def shared_libraries(directory, target):
    suffix = ".dll" if target.startswith("windows") else ".dylib" if target.startswith("macos") else ".so"
    return sorted(p for p in directory.rglob("*") if p.is_file()
                  and (p.name.endswith(suffix) or (suffix == ".so" and ".so." in p.name)))


def verify_runtime_manifest(directory, component, target):
    name = f"mpv-natives-{target}.txt" if component == "mpv" else "ffmpeg-natives.txt"
    manifest = directory / name
    entries = [s.strip() for s in manifest.read_text().splitlines() if s.strip()]
    if not entries:
        raise ValueError(f"Empty native manifest: {manifest}")
    for entry in entries:
        relative = PurePosixPath(entry)
        if relative.is_absolute() or ".." in relative.parts or "\\" in entry or ":" in entry:
            raise ValueError(f"Invalid native manifest entry: {entry}")
        path = directory.joinpath(*relative.parts)
        if not path.is_file():
            raise ValueError(f"Missing native manifest entry: {path}")
    for path in shared_libraries(directory, target):
        verify_binary_arch(path, target)


def mpv_jni_exports(compiler, flags, include_dirs, env=None):
    # Preprocess with the actual target toolchain: native entry points are gated
    # by _WIN32/__APPLE__/__linux__. Checking all source declarations on every
    # platform would incorrectly require exports for the other OS backends.
    source = run([compiler, *flags, "-E", "-P",
                  *(arg for directory in include_dirs for arg in ("-I", directory)),
                  ROOT / "mediamp-mpv/src/cpp/jni.cpp"], env=env,
                 capture_output=True, text=True).stdout
    methods = sorted(set(re.findall(r"\bJava_org_openani_mediamp_mpv_\w+", source)))
    if len(methods) < 20:
        raise RuntimeError("Could not derive JNI exports from target-preprocessed source")
    return methods


def write_provenance(directory, component, target, version, **fields):
    values = {"version": version, "component": component, "platform": target,
              "base-runtime": BASE_VERSION, "base-sha256": BASE_SHA256[component][target], **fields}
    path = directory / "META-INF/mediamp-tao-native-build.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8", newline="\n")


def package_runtime(directory, component, target, version):
    output_dir = ROOT / f"mediamp-{component}/build/prebuilt-runtime-jars" / target
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"mediamp-{component}-runtime-{version}-{target}.jar"
    pending = output.with_suffix(".jar.tmp")
    try:
        with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(directory).as_posix())
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    print(output)
    return output


POSIX_LOAD_PROBE = r'''
#include <dlfcn.h>
#include <cstdio>
#include <mpv/client.h>
int main(int argc, char **argv) {
    if (argc != 3) return 1;
    void *jni = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!jni) { std::fprintf(stderr, "JNI dlopen: %s\n", dlerror()); return 2; }
    const char *methods[] = { @METHODS@ };
    for (const char *method : methods) {
        if (!dlsym(jni, method)) { std::fprintf(stderr, "Missing JNI export: %s\n", method); return 3; }
    }
    void *mpv = dlopen(argv[2], RTLD_NOW | RTLD_LOCAL);
    if (!mpv) { std::fprintf(stderr, "mpv dlopen: %s\n", dlerror()); return 4; }
    auto api = reinterpret_cast<decltype(&mpv_client_api_version)>(dlsym(mpv, "mpv_client_api_version"));
    auto create = reinterpret_cast<decltype(&mpv_create)>(dlsym(mpv, "mpv_create"));
    auto option = reinterpret_cast<decltype(&mpv_set_option_string)>(dlsym(mpv, "mpv_set_option_string"));
    auto init = reinterpret_cast<decltype(&mpv_initialize)>(dlsym(mpv, "mpv_initialize"));
    auto destroy = reinterpret_cast<decltype(&mpv_terminate_destroy)>(dlsym(mpv, "mpv_terminate_destroy"));
    if (!api || !create || !option || !init || !destroy) return 5;
    if ((api() >> 16) != (MPV_CLIENT_API_VERSION >> 16) || api() < MPV_CLIENT_API_VERSION) return 6;
    mpv_handle *handle = create();
    if (!handle) return 7;
    int rc = option(handle, "config", "no");
    if (rc >= 0) rc = option(handle, "vo", "null");
    if (rc >= 0) rc = option(handle, "ao", "null");
    if (rc >= 0) rc = init(handle);
    destroy(handle);
    if (rc < 0) { std::fprintf(stderr, "mpv headless init: %d\n", rc); return 8; }
    std::printf("Verified all %zu JNI exports; mpv initialized; API=%lu\n", sizeof(methods)/sizeof(methods[0]), api());
    dlclose(mpv); dlclose(jni); return 0;
}
'''


def posix_load_probe(scratch, runtime, compiler, flags, jni_name, mpv_name, methods):
    source = scratch / "load-probe.cpp"
    source.write_text(POSIX_LOAD_PROBE.replace("@METHODS@", ", ".join(f'"{m}"' for m in methods)))
    executable = scratch / "load-probe"
    run([compiler, *flags, "-I", ROOT / "mediamp-mpv/mpv/include", source, "-o", executable])
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("DYLD_", "LD_"))}
    # Linux SONAME resolution needs the packaged directory; no developer paths.
    if jni_name.endswith(".so"):
        env["LD_LIBRARY_PATH"] = str(runtime)
    run([executable, runtime / jni_name, runtime / mpv_name], env=env, cwd=scratch)
