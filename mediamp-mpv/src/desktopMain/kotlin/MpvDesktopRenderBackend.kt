package org.openani.mediamp.mpv

import org.jetbrains.skiko.OS
import org.jetbrains.skiko.hostOs
import org.openani.mediamp.mpv.internal.D3D11TaoSurfaceBackend
import org.openani.mediamp.mpv.internal.MacosSurfaceRingBackend
import org.openani.mediamp.mpv.internal.MpvSurfaceBackend
import org.openani.mediamp.mpv.internal.WindowsOpenGLSurfaceBackend
import org.openani.mediamp.mpv.internal.currentSurfaceBackend

/**
 * Explicit producer selection for a desktop window host. This is a desktop-only API;
 * it neither loads a window framework nor changes the Android/iOS player API.
 */
enum class MpvDesktopRenderBackend {
    /** Existing Skiko/AWT selection, including its Windows renderer fallback. */
    AWT,
    /** macOS CGL/IOSurface producer, consumed on a host-provided Metal device. */
    METAL,
    /** Windows D3D11 producer with a stable legacy shared texture and keyed mutex. */
    TAO_D3D11,
    /** Windows compatibility producer; each frame passes through CPU memory. */
    WINDOWS_OPENGL_READBACK,
}

internal fun MpvDesktopRenderBackend.resolve(os: OS = hostOs): MpvSurfaceBackend? = when (this) {
    MpvDesktopRenderBackend.AWT -> currentSurfaceBackend()
    MpvDesktopRenderBackend.METAL -> {
        require(os == OS.MacOS) { "The Metal mediamp backend requires macOS (host is $os)" }
        MacosSurfaceRingBackend
    }
    MpvDesktopRenderBackend.TAO_D3D11 -> {
        require(os == OS.Windows) { "The TAO D3D11 mediamp backend requires Windows (host is $os)" }
        D3D11TaoSurfaceBackend
    }
    MpvDesktopRenderBackend.WINDOWS_OPENGL_READBACK -> {
        require(os == OS.Windows) { "The WGL readback mediamp backend requires Windows (host is $os)" }
        WindowsOpenGLSurfaceBackend
    }
}
