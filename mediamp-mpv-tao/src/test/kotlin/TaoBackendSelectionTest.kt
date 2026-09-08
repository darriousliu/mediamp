package org.openani.mediamp.mpv.tao

import org.jetbrains.skiko.OS
import org.openani.mediamp.mpv.MpvDesktopRenderBackend
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class TaoBackendSelectionTest {
    @Test
    fun metalSelectionDoesNotDependOnSkikoAwtRendererProperty() {
        val original = System.getProperty("skiko.renderApi")
        try {
            for (awtApi in listOf("OPENGL", "SOFTWARE_FAST", "DIRECT3D")) {
                System.setProperty("skiko.renderApi", awtApi)
                assertEquals(MpvDesktopRenderBackend.METAL, taoRenderBackend(OS.MacOS))
            }
        } finally {
            if (original == null) System.clearProperty("skiko.renderApi")
            else System.setProperty("skiko.renderApi", original)
        }
    }

    @Test
    fun windowsDoesNotSelectTheAwtD3d12Consumer() {
        assertEquals(MpvDesktopRenderBackend.TAO_D3D11, taoRenderBackend(OS.Windows))
        assertEquals(MpvDesktopRenderBackend.WINDOWS_OPENGL_READBACK, taoRenderBackend(OS.Windows, true))
    }

    @Test
    fun linuxCannotSilentlyFallThroughToGlx() {
        assertFailsWith<IllegalStateException> { taoRenderBackend(OS.Linux) }
    }
}
