package org.openani.mediamp.mpv

import org.jetbrains.skiko.OS
import org.openani.mediamp.mpv.internal.D3D11TaoSurfaceBackend
import org.openani.mediamp.mpv.internal.MacosSurfaceRingBackend
import org.openani.mediamp.mpv.internal.WindowsOpenGLSurfaceBackend
import kotlin.test.Test
import kotlin.test.assertFailsWith
import kotlin.test.assertSame

class MpvDesktopRenderBackendTest {
    @Test
    fun explicitBackendsResolveToTheCorrectNativeProtocol() {
        assertSame(MacosSurfaceRingBackend, MpvDesktopRenderBackend.METAL.resolve(OS.MacOS))
        assertSame(D3D11TaoSurfaceBackend, MpvDesktopRenderBackend.TAO_D3D11.resolve(OS.Windows))
        assertSame(WindowsOpenGLSurfaceBackend, MpvDesktopRenderBackend.WINDOWS_OPENGL_READBACK.resolve(OS.Windows))
    }

    @Test
    fun wrongPlatformCannotReachAnIncompatibleJniContract() {
        assertFailsWith<IllegalArgumentException> { MpvDesktopRenderBackend.METAL.resolve(OS.Windows) }
        assertFailsWith<IllegalArgumentException> { MpvDesktopRenderBackend.TAO_D3D11.resolve(OS.MacOS) }
        assertFailsWith<IllegalArgumentException> { MpvDesktopRenderBackend.WINDOWS_OPENGL_READBACK.resolve(OS.Linux) }
    }
}
