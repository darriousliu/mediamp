@file:OptIn(org.openani.mediamp.InternalMediampApi::class)

package org.openani.mediamp.mpv.tao

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.layer.GraphicsLayer
import androidx.compose.ui.graphics.layer.drawLayer
import androidx.compose.ui.graphics.rememberGraphicsLayer
import androidx.compose.ui.graphics.toAwtImage
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.WindowPlacement
import androidx.compose.ui.window.rememberWindowState
import dev.nucleusframework.application.DecoratedWindow
import dev.nucleusframework.application.NucleusBackend
import dev.nucleusframework.application.nucleusApplication
import dev.nucleusframework.window.NucleusDecoratedWindowTheme
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeout
import org.openani.mediamp.MediaStatus
import org.openani.mediamp.features.FramePreview
import org.openani.mediamp.features.Screenshots
import org.openani.mediamp.mpv.MpvMediampPlayer
import org.openani.mediamp.mpv.MPVHandle
import org.openani.mediamp.source.UriMediaData
import java.io.File
import javax.imageio.ImageIO
import kotlin.math.abs

/**
 * Actual window integration check, opt-in via :mediamp-mpv-tao:taoSmoke. Generates a
 * local 24-second test pattern when no video is supplied. A non-empty PNG validates
 * the native video pipeline; visually checking the real window is still necessary to
 * prove final composition (the PNG is deliberately not labelled a window screenshot).
 */
fun main() {
    val output = File(System.getProperty("mediamp.tao.smoke.output")).apply { mkdirs() }
    val report = File(output, "report.txt")
    report.writeText("RUNNING: real TAO window integration check\n")
    val video = prepareVideo(output)
    System.getProperty("mediamp.tao.smoke.native.dir")?.takeIf { it.isNotBlank() }?.let {
        MpvMediampPlayer.prepareLibraries(it, extractRuntimeLibrary = false)
    }
    var failure: Throwable? = null
    nucleusApplication(backend = NucleusBackend.Tao, enableSingleInstance = false) {
        val windowState = rememberWindowState(size = DpSize(720.dp, 480.dp))
        val scope = rememberCoroutineScope()
        val factory = remember { TaoMpvMediampPlayerFactory() }
        var player by remember { mutableStateOf(factory.create(Unit, scope.coroutineContext)) }
        var showSurface by remember { mutableStateOf(true) }
        var stage by remember { mutableStateOf("Opening local test pattern") }
        var compositionLayer by remember { mutableStateOf<GraphicsLayer?>(null) }
        DisposableEffect(Unit) {
            onDispose { player.close() }
        }
        NucleusDecoratedWindowTheme(isDark = true) {
            DecoratedWindow(onCloseRequest = ::exitApplication, state = windowState, title = "mediamp TAO smoke") {
                val captureLayer = rememberGraphicsLayer()
                DisposableEffect(captureLayer) {
                    compositionLayer = captureLayer
                    onDispose { compositionLayer = null }
                }
                LaunchedEffect(nucleusWindow) {
                    delay(200)
                    nucleusWindow.toFront()
                    nucleusWindow.requestFocus()
                }
                Box(
                    Modifier.fillMaxSize().drawWithContent {
                        captureLayer.record { this@drawWithContent.drawContent() }
                        drawLayer(captureLayer)
                    }.background(Color(0xFF111827)),
                ) {
                    if (showSurface) TaoMpvMediampPlayerSurface(player, Modifier.fillMaxSize())
                    BasicText(stage, style = TextStyle(color = Color.White, fontSize = 18.sp))
                }
            }
        }
        LaunchedEffect(Unit) {
            fun passed(name: String) {
                report.appendText("PASS: $name\n")
                println("TAO_SMOKE_PASS: $name")
            }
            try {
                withTimeout(60_000) {
                    player.setMediaData(UriMediaData(video.toURI().toString()), playWhenReady = true)
                    player.currentPositionMillis.first { it >= 500 }
                    check(player.state.value.mediaStatus == MediaStatus.Ready)
                    delay(500)
                    val firstSize = captureVideoFrame(player, File(output, "01-playing.png"))
                    passed("playing local video, nonempty native video frame ${firstSize.first}x${firstSize.second}")
                    val composedImage = checkNotNull(compositionLayer).toImageBitmap().toAwtImage()
                    ImageIO.write(composedImage, "png", File(output, "06-compose-content.png"))
                    passed("recorded whole Compose content, including video and white overlay")
                    val handle = player.impl as MPVHandle
                    val audioOutput = handle.getPropertyString("current-ao")
                    report.appendText("AUDIO: current-ao=$audioOutput, channels=${handle.getPropertyInt("audio-params/channel-count")}\n")
                    println("TAO_SMOKE_AUDIO: current-ao=$audioOutput")

                    stage = "Paused; verifying the clock stays still"
                    player.pause()
                    delay(300)
                    val pausedAt = player.currentPositionMillis.value
                    delay(500)
                    check(abs(player.currentPositionMillis.value - pausedAt) < 150) { "Pause did not stop the clock" }
                    passed("pause")

                    stage = "Seeking to 6 seconds"
                    player.seekTo(6_000)
                    player.currentPositionMillis.first { it in 5_800..6_500 }
                    captureVideoFrame(player, File(output, "02-seek.png"))
                    passed("seek while paused")

                    stage = "Resizing while paused"
                    windowState.size = DpSize(1000.dp, 650.dp)
                    delay(1_200)
                    val resized = captureVideoFrame(player, File(output, "03-resized.png"))
                    check(resized.first > firstSize.first && resized.second > firstSize.second) {
                        "Producer surface did not resize: $firstSize -> $resized"
                    }
                    passed("resize while paused: ${resized.first}x${resized.second}")

                    stage = "Fullscreen and restore"
                    windowState.placement = WindowPlacement.Fullscreen
                    delay(700)
                    windowState.placement = WindowPlacement.Floating
                    delay(700)
                    passed("fullscreen and restore")

                    stage = "Detaching and attaching the same player"
                    showSurface = false
                    delay(350)
                    showSurface = true
                    delay(800)
                    captureVideoFrame(player, File(output, "04-reattached.png"))
                    passed("surface disposal and reattachment")

                    stage = "Generating an independent preview frame"
                    val preview = player.features.getOrFail(FramePreview).getPreviewFrame(9_000, 240, 135)
                    check(preview != null) { "Frame preview returned null" }
                    passed("frame preview from selected backend")

                    stage = "Closing player while its surface is attached"
                    player.close()
                    check(player.state.value.mediaStatus == MediaStatus.Released)
                    player = factory.create(Unit, scope.coroutineContext)
                    player.setMediaData(UriMediaData(video.toURI().toString()), playWhenReady = true, startPositionMillis = 2_000)
                    player.currentPositionMillis.first { it >= 2_500 }
                    captureVideoFrame(player, File(output, "05-reopened.png"))
                    passed("release with attached surface; new player opens and renders")

                    stage = "Checks passed — inspect the animated video and white overlay"
                    // Leaves a deterministic inspection interval for an external window
                    // capture. This harness does not claim its native PNG is screen output.
                    delay(15_000)
                    player.close()
                    report.appendText("SUCCESS: playback/seek/resize/disposal/preview/reopen; final window composition requires external visual inspection\n")
                }
            } catch (error: Throwable) {
                failure = error
                report.appendText("FAIL: ${error.stackTraceToString()}\n")
                error.printStackTrace()
                runCatching { player.close() }
                // TAO's event loop exits the process with status 0, so throwing after
                // nucleusApplication returns cannot turn a failed check into failure.
                kotlin.system.exitProcess(1)
            } finally {
                exitApplication()
            }
        }
    }
    failure?.let { throw it }
}

private suspend fun captureVideoFrame(player: MpvMediampPlayer, file: File): Pair<Int, Int> {
    player.features.getOrFail(Screenshots).takeScreenshot(file.absolutePath)
    check(file.isFile && file.length() > 1_000) { "Video frame PNG was not written: $file" }
    val frame = checkNotNull(ImageIO.read(file)) { "Invalid PNG: $file" }
    check(frame.width > 100 && frame.height > 100)
    val colors = mutableSetOf<Int>()
    for (y in 0 until frame.height step maxOf(1, frame.height / 20)) {
        for (x in 0 until frame.width step maxOf(1, frame.width / 20)) colors += frame.getRGB(x, y)
    }
    check(colors.size > 12) { "Expected a colored video frame, got only ${colors.size} sampled colors" }
    return frame.width to frame.height
}

private fun prepareVideo(output: File): File {
    System.getProperty("mediamp.tao.smoke.video")?.takeIf { it.isNotBlank() }?.let {
        return File(it).also { video -> require(video.isFile) { "Video does not exist: $video" } }
    }
    val video = File(output, "test-pattern.mp4")
    val ffmpeg = listOf("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
        .firstOrNull { File(it).canExecute() } ?: "ffmpeg"
    val result = ProcessBuilder(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=960x540:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "24", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-af", "volume=0.01", video.absolutePath,
    ).inheritIO().start().waitFor()
    check(result == 0 && video.isFile) { "Could not generate test video; provide -Pmediamp.tao.smoke.video=/absolute/video.mp4" }
    return video
}
