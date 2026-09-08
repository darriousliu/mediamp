package org.openani.mediamp.mpv.internal

import org.jetbrains.skia.DirectContext
import org.jetbrains.skia.Image
import org.jetbrains.skiko.SkiaLayer
import org.openani.mediamp.InternalMediampApi
import org.openani.mediamp.mpv.MpvSharedTextureFrame
import org.openani.mediamp.mpv.nAckRetiredTaoD3D11Texture
import org.openani.mediamp.mpv.nCreateRenderContextTaoD3D11
import org.openani.mediamp.mpv.nDestroyRenderContextD3D11
import org.openani.mediamp.mpv.nGetFrameStateD3D11
import org.openani.mediamp.mpv.nGetSharedTextureTaoD3D11
import org.openani.mediamp.mpv.nGetRetiredGenerationTaoD3D11
import org.openani.mediamp.mpv.nHasD3D11Surface
import org.openani.mediamp.mpv.nReadSurfacePixelsD3D11
import org.openani.mediamp.mpv.nSaveSurfacePngD3D11
import org.openani.mediamp.mpv.nSetSurfaceConfigTaoD3D11
import org.openani.mediamp.mpv.utils.SkiaRenderDeviceInterop

/** TAO consumes a legacy D3D11 handle; no Skiko D3D12 device/texture is involved. */
@OptIn(InternalMediampApi::class)
internal object D3D11TaoSurfaceBackend : MpvSurfaceBackend {
    override val rendererName = "TAO D3D11 legacy keyed-mutex"
    override fun createRenderContext(ptr: Long) = nCreateRenderContextTaoD3D11(ptr)
    override fun destroyRenderContext(ptr: Long) = nDestroyRenderContextD3D11(ptr)
    override fun setSurfaceConfig(ptr: Long, width: Int, height: Int, devicePtr: Long) =
        nSetSurfaceConfigTaoD3D11(ptr, width, height)
    override fun getFrameState(ptr: Long) = nGetFrameStateD3D11(ptr)
    override fun hasSurface(ptr: Long) = nHasD3D11Surface(ptr)
    override fun saveSurfacePng(ptr: Long, path: String) = nSaveSurfacePngD3D11(ptr, path)
    override fun readSurfacePixels(ptr: Long, dims: IntArray) = nReadSurfacePixelsD3D11(ptr, dims)
    override fun createSkiaInterop(layer: SkiaLayer): SkiaRenderDeviceInterop =
        error("TAO D3D11 uses TextureView, not an AWT SkiaLayer")

    fun currentSharedTexture(ptr: Long): MpvSharedTextureFrame? {
        val state = getFrameState(ptr)
        val generation = ((state ushr 48) and 0xFFFF).toInt()
        val index = ((state ushr 44) and 0xF).toInt()
        val width = ((state ushr 30) and 0x3FFF).toInt()
        val height = ((state ushr 16) and 0x3FFF).toInt()
        if (index == 0xF || width <= 0 || height <= 0) return null
        // The producer may swap between these calls: the JNI accessor validates the
        // requested generation rather than returning a handle from a different size.
        val sharedHandle = nGetSharedTextureTaoD3D11(ptr, generation)
        if (sharedHandle == 0L) return null
        return MpvSharedTextureFrame(generation, sharedHandle, width, height)
    }

    fun acknowledge(ptr: Long, generation: Int): Boolean = nAckRetiredTaoD3D11Texture(ptr, generation)
    fun retiredGeneration(ptr: Long): Int = nGetRetiredGenerationTaoD3D11(ptr)

    override fun createSurfaceConsumer(handlePtr: Long): MpvSurfaceConsumer = object : MpvSurfaceConsumer {
        override fun requestSurface(width: Int, height: Int, devicePtr: Long) =
            setSurfaceConfig(handlePtr, width, height, 0L)
        override fun refreshDeviceIfChanged(devicePtr: Long) = Unit
        override fun invalidateForRenderEnvironmentChange() = Unit
        override fun currentFrameImage(directContext: DirectContext, leasedFrameState: Long?): Image? = null
        // TextureView owns the imported consumer image; disposal drops its lease
        // before this surface is deactivated. The native side owns legacy handles.
        override fun release() { setSurfaceConfig(handlePtr, 0, 0, 0L) }
    }
}
