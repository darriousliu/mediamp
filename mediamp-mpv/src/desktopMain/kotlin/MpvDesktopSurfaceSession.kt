package org.openani.mediamp.mpv

import org.jetbrains.skia.DirectContext
import org.jetbrains.skia.Image
import org.openani.mediamp.InternalMediampApi
import org.openani.mediamp.mpv.internal.D3D11TaoSurfaceBackend

/**
 * Borrowed GPU context of one desktop surface. Implementations execute synchronously
 * on the context's owning GPU thread, with an OpenGL context current where needed.
 * Calls enter from the composition thread, including [withGpuContext] during disposal.
 */
@InternalMediampApi
interface MpvDesktopGpuHost {
    val directContext: DirectContext
    /** A borrowed MTLDevice pointer for METAL; zero for CPU/legacy-texture consumers. */
    val metalDevicePtr: Long
    fun <T> withGpuContext(action: () -> T): T?
}

/** Stable, borrowed Windows export; its handle must never be passed to CloseHandle. */
@InternalMediampApi
data class MpvSharedTextureFrame(val generation: Int, val handle: Long, val width: Int, val height: Int)

/**
 * One surface attachment. Closing is idempotent and invalidates future configure/draw
 * calls, including delayed resize work from a replaced composition. The player closes
 * its attachment before asynchronous native teardown if the player is released first.
 */
@InternalMediampApi
class MpvDesktopSurfaceSession internal constructor(
    private val player: MpvMediampPlayer,
    private val host: MpvDesktopGpuHost,
    onFrameAvailable: () -> Unit,
) : AutoCloseable {
    @Volatile private var closed = false
    private val compositionThread = Thread.currentThread()
    private val textureGenerations = MpvTextureGenerationTracker()

    init {
        check(player.renderContextLifecycle?.createEagerly() == true) {
            "Could not create ${player.desktopRenderBackend} mpv render context"
        }
        check(player.setRenderUpdateListener { if (!closed) onFrameAvailable() }) {
            "Could not subscribe to mpv frame updates"
        }
    }

    fun requestSurface(width: Int, height: Int): Boolean {
        checkThread()
        if (closed || width <= 0 || height <= 0) return false
        return host.withGpuContext {
            if (closed) false else player.requestSurface(width, height, host.metalDevicePtr)
        } ?: false
    }

    /** The returned image is borrowed until the next draw/disposal; do not close it. */
    fun currentFrameImage(): Image? {
        checkThread()
        if (closed) return null
        return host.withGpuContext {
            if (closed) return@withGpuContext null
            player.refreshDeviceIfChanged(host.metalDevicePtr)
            val metal = player.desktopRenderBackend == MpvDesktopRenderBackend.METAL
            val leasedState = if (metal) nAcquireFrameStateMacos(player.handle.ptr) else null
            try {
                player.currentFrameImage(host.directContext, leasedState)?.also {
                // Finish the IOSurface -> owned Skia texture copy before the native
                // producer can reuse that ring slot and before scene replay on TAO's
                // separate Metal render thread. The returned owned image can then be
                // recorded in the Compose display list without touching borrowed GPU
                // objects from the composition thread.
                    if (metal) {
                        host.directContext.flush()
                        host.directContext.submit(syncCpu = true)
                    }
                }
            } finally {
                if (leasedState != null && ((leasedState ushr 44) and 0xF) != 0xFL) {
                    nReleaseFrameMacos(player.handle.ptr, leasedState)
                }
            }
        }
    }

    fun currentSharedTexture(): MpvSharedTextureFrame? {
        checkThread()
        if (closed) return null
        check(player.desktopRenderBackend == MpvDesktopRenderBackend.TAO_D3D11)
        val frame = D3D11TaoSurfaceBackend.currentSharedTexture(player.handle.ptr) ?: return null
        textureGenerations.offer(frame.generation)
        val retired = D3D11TaoSurfaceBackend.retiredGeneration(player.handle.ptr)
        if (retired >= 0 && textureGenerations.canRetire(retired)) {
            D3D11TaoSurfaceBackend.acknowledge(player.handle.ptr, retired)
        }
        return frame
    }

    /** Marks a source committed to composition; balanced after its child import closes. */
    fun retainSharedTexture(generation: Int) {
        checkThread()
        if (!closed) textureGenerations.attach(generation)
    }

    /** Called after TextureView has dropped the import for exactly this generation. */
    fun acknowledgeSharedTexture(generation: Int) {
        checkThread()
        if (!closed) {
            textureGenerations.detach(generation)
            D3D11TaoSurfaceBackend.acknowledge(player.handle.ptr, generation)
        }
    }

    override fun close() {
        checkThread()
        if (closed) return
        closed = true
        try {
            player.setRenderUpdateListener(null)
            if (player.desktopRenderBackend != MpvDesktopRenderBackend.METAL) {
                // Legacy TextureView owns its import, and readback images are CPU
                // objects: deactivation must still run after GL became unbindable.
                player.releaseSurface()
            } else {
                host.withGpuContext {
                    host.directContext.flush()
                    host.directContext.submit(syncCpu = true)
                    player.releaseSurface()
                }
            }
        } finally {
            player.desktopSurfaceDetached(this)
        }
    }

    private fun checkThread() {
        check(Thread.currentThread() === compositionThread) {
            "Desktop surface sessions must be used and disposed on their composition thread"
        }
    }
}

/** Separates published descriptors from imports actually committed to composition. */
internal class MpvTextureGenerationTracker {
    private var offered: Int? = null
    private val attached = mutableSetOf<Int>()
    fun offer(generation: Int) { offered = generation }
    fun attach(generation: Int) { attached += generation }
    fun detach(generation: Int) {
        attached -= generation
        if (offered == generation) offered = null
    }
    fun canRetire(generation: Int): Boolean = generation != offered && generation !in attached
}
