package org.openani.mediamp.mpv.tao

import org.jetbrains.skiko.OS
import org.jetbrains.skiko.hostOs
import org.openani.mediamp.MediampPlayerFactory
import org.openani.mediamp.mpv.MpvDesktopRenderBackend
import org.openani.mediamp.mpv.MpvMediampPlayer
import kotlin.coroutines.CoroutineContext
import kotlin.reflect.KClass

/**
 * Create after nucleusApplication initializes TAO's Main dispatcher. Selection is
 * explicit: SkikoProperties.renderApi describes AWT and cannot select a TAO producer.
 * No ServiceLoader registration is installed, so existing AWT factories remain intact.
 *
 * Windows legacy-texture mode requires the matching -tao native runtime. The optional
 * readback mode supports validating an existing Windows runtime at a CPU-copy cost.
 * Linux is rejected until the EGL producer and context-loss recovery are implemented.
 */
public class TaoMpvMediampPlayerFactory(
    private val windowsReadbackCompatibility: Boolean = false,
) : MediampPlayerFactory<MpvMediampPlayer> {
    override val forClass: KClass<MpvMediampPlayer> = MpvMediampPlayer::class

    override fun create(context: Any, parentCoroutineContext: CoroutineContext): MpvMediampPlayer =
        MpvMediampPlayer(
            context,
            parentCoroutineContext,
            desktopRenderBackend = taoRenderBackend(hostOs, windowsReadbackCompatibility),
        )
}

internal fun taoRenderBackend(os: OS, windowsReadbackCompatibility: Boolean = false): MpvDesktopRenderBackend =
    when (os) {
        OS.MacOS -> MpvDesktopRenderBackend.METAL
        OS.Windows -> if (windowsReadbackCompatibility) {
            MpvDesktopRenderBackend.WINDOWS_OPENGL_READBACK
        } else {
            MpvDesktopRenderBackend.TAO_D3D11
        }
        else -> error("mediamp TAO requires macOS or Windows; $os needs the EGL producer and context-loss recovery")
    }
