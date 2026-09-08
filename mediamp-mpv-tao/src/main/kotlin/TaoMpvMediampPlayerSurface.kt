@file:OptIn(org.openani.mediamp.InternalMediampApi::class)

package org.openani.mediamp.mpv.tao

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.unit.IntSize
import dev.nucleusframework.window.tao.TaoGpuRenderContext
import dev.nucleusframework.window.tao.TaoMetalRenderContext
import dev.nucleusframework.window.tao.TaoOpenGlRenderContext
import dev.nucleusframework.window.tao.TextureView
import dev.nucleusframework.window.tao.nucleusD3D11SharedTextureSource
import dev.nucleusframework.window.tao.rememberTaoGpuRenderContext
import dev.nucleusframework.window.tao.rememberTextureViewController
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.filter
import org.jetbrains.skia.DirectContext
import org.jetbrains.skia.Rect
import org.jetbrains.skia.SamplingMode
import org.openani.mediamp.mpv.MpvDesktopGpuHost
import org.openani.mediamp.mpv.MpvDesktopRenderBackend
import org.openani.mediamp.mpv.MpvDesktopSurfaceSession
import org.openani.mediamp.mpv.MpvMediampPlayer
import org.openani.mediamp.mpv.MpvSharedTextureFrame

/** Video remains in the Compose scene, allowing normal controls, clipping and overlays. */
@Composable
public fun TaoMpvMediampPlayerSurface(player: MpvMediampPlayer, modifier: Modifier = Modifier) {
    require(player.desktopRenderBackend != MpvDesktopRenderBackend.AWT) {
        "Use TaoMpvMediampPlayerFactory to select the TAO producer before creating the player"
    }
    val gpuContext = rememberTaoGpuRenderContext()
    if (gpuContext == null) {
        Box(modifier)
        return
    }
    val host = remember(gpuContext) { TaoGpuHost(gpuContext) }
    require(player.desktopRenderBackend != MpvDesktopRenderBackend.METAL || gpuContext is TaoMetalRenderContext) {
        "The Metal mpv producer requires a TAO Metal surface"
    }
    val notifications = remember(player, host) { Channel<Unit>(Channel.CONFLATED) }
    val textureController = rememberTextureViewController()
    var session by remember(player, host) { mutableStateOf<MpvDesktopSurfaceSession?>(null) }
    var size by remember(player, host) { mutableStateOf(IntSize.Zero) }
    var frameTick by remember(player, host) { mutableLongStateOf(0L) }
    var sharedTexture by remember(player, host) { mutableStateOf<MpvSharedTextureFrame?>(null) }

    // Key on context identity. The old effect is disposed (on the old GPU executor)
    // before a new session can configure the player's ring for another device.
    DisposableEffect(player, host) {
        val attached = player.attachDesktopSurface(host) {
            notifications.trySend(Unit)
            textureController.markFrameAvailable()
        }
        session = attached
        onDispose {
            notifications.close()
            attached.close()
        }
    }

    LaunchedEffect(session, notifications) {
        val attached = session ?: return@LaunchedEffect
        for (ignored in notifications) {
            frameTick++
            if (player.desktopRenderBackend == MpvDesktopRenderBackend.TAO_D3D11) {
                attached.currentSharedTexture()?.let { sharedTexture = it }
            }
        }
    }

    LaunchedEffect(session) {
        val attached = session ?: return@LaunchedEffect
        var configured = false
        snapshotFlow { size }
            .filter { it.width > 0 && it.height > 0 }
            .collectLatest { newSize ->
                if (configured) delay(150)
                while (!attached.requestSurface(newSize.width, newSize.height)) delay(50)
                configured = true
            }
    }

    val surfaceModifier = modifier.onSizeChanged { size = it }
    if (player.desktopRenderBackend == MpvDesktopRenderBackend.TAO_D3D11) {
        val frame = sharedTexture
        val source = remember(frame) {
            frame?.let { nucleusD3D11SharedTextureSource(it.handle, it.width, it.height) }
        }
        // Registered before TextureView so reverse-order forgetting closes the child
        // import first. Acknowledge only the generation actually imported, never the
        // newest generation observed by an asynchronous frame callback.
        DisposableEffect(session, frame) {
            val attached = session
            frame?.let { attached?.retainSharedTexture(it.generation) }
            onDispose { frame?.let { attached?.acknowledgeSharedTexture(it.generation) } }
        }
        TextureView(source, surfaceModifier, textureController, contentScale = ContentScale.Fit)
    } else {
        Canvas(surfaceModifier) {
            frameTick // subscribe only the draw pass to per-frame invalidation
            val frame = session?.currentFrameImage() ?: return@Canvas
            if (size.width <= 0 || size.height <= 0) return@Canvas
            val scale = minOf(size.width / frame.width.toFloat(), size.height / frame.height.toFloat())
            val width = frame.width * scale
            val height = frame.height * scale
            drawIntoCanvas { canvas ->
                canvas.nativeCanvas.drawImageRect(
                    frame,
                    Rect.makeWH(frame.width.toFloat(), frame.height.toFloat()),
                    Rect.makeXYWH((size.width - width) / 2, (size.height - height) / 2, width, height),
                    SamplingMode.LINEAR,
                    null,
                    true,
                )
            }
        }
    }
}

/** All borrowed handles stay confined to the lifetime of this context instance. */
private class TaoGpuHost(private val context: TaoGpuRenderContext) : MpvDesktopGpuHost {
    // Read borrowed handles on the composition thread once, then use them only
    // inside the owning executor for the lifetime of this context identity.
    override val directContext: DirectContext = context.skiaContext
    override val metalDevicePtr: Long = (context as? TaoMetalRenderContext)?.metalDevicePtr ?: 0L
    override fun <T> withGpuContext(action: () -> T): T? = when (context) {
        is TaoMetalRenderContext -> context.runOnGpuThread(action)
        is TaoOpenGlRenderContext -> context.withContextCurrent(action)
        else -> error("Unsupported TAO GPU context: ${context::class.qualifiedName}")
    }
}
