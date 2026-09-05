/*
 * Copyright (C) 2024-2026 OpenAni and contributors.
 *
 * Use of this source code is governed by the Apache License version 2 license, which can be found at the following link.
 *
 * https://github.com/open-ani/mediamp/blob/main/LICENSE
 */

package org.openani.mediamp.exoplayer.internal

import androidx.media3.common.C
import androidx.media3.exoplayer.ExoPlayer
import kotlin.math.roundToInt

internal data class VideoDisplaySize(val width: Int, val height: Int)

/**
 * Display size of the current video, in pixels, with the pixel aspect ratio and rotation applied.
 * Returns null while no video track is selected or its size is unknown.
 *
 * Once ExoPlayer.setVideoEffects has been called, even with an empty list, media3 1.9.0 routes
 * video through the VideoSink path and stops reporting VideoSize to Player.Listener:
 * MediaCodecVideoRenderer calls maybeNotifyVideoSizeChanged only while videoSink is null, and the
 * sink listener it installs in configureVideoSink has an empty onVideoSizeChanged (b/292111083).
 * The selected video track's Format is then the only source of the size.
 */
// TODO: drop the Format fallback once media3 reports video size on the VideoSink path.
//  media3 removed that reporting on purpose, to keep the first frame rendered and the video
//  restored after the screen goes off, so b/292111083 is a known-incomplete state rather than
//  an oversight, and the sink path is not settled yet.
//  Video size is not the only thing that changes there: MediaCodecVideoRenderer branches on
//  whether videoSink is set in over thirty places, covering decoder frame dropping, the frame
//  release control reset after a seek, and which surface the codec decodes into. Anything else
//  this module reads from the renderer rather than from the track Format deserves the same
//  scrutiny before it is trusted under video effects.
internal fun ExoPlayer.videoDisplaySizeOrNull(): VideoDisplaySize? {
    val videoSize = this.videoSize
    if (videoSize.width > 0 && videoSize.height > 0) {
        // VideoSize is already rotated.
        return VideoDisplaySize(
            (videoSize.width * videoSize.pixelWidthHeightRatio).roundToInt(),
            videoSize.height,
        )
    }

    val group = currentTracks.groups.firstOrNull { it.type == C.TRACK_TYPE_VIDEO && it.isSelected }
        ?: return null
    val selectedIndex = (0 until group.length).firstOrNull { group.isTrackSelected(it) } ?: 0
    val format = group.getTrackFormat(selectedIndex)
    return displaySizeOrNull(
        format.width,
        format.height,
        format.pixelWidthHeightRatio,
        format.rotationDegrees,
    )
}

/**
 * Display size of a video frame stored as [storedWidth] x [storedHeight], or null if either is
 * unknown.
 *
 * [pixelWidthHeightRatio] describes the stored frame, so it scales the stored width; after a
 * quarter-turn that axis is the display height. Format normalizes an unknown ratio to 1
 * (Format.NO_VALUE never reaches here), so there is nothing to guard against.
 */
internal fun displaySizeOrNull(
    storedWidth: Int,
    storedHeight: Int,
    pixelWidthHeightRatio: Float,
    rotationDegrees: Int,
): VideoDisplaySize? {
    if (storedWidth <= 0 || storedHeight <= 0) return null
    val width = (storedWidth * pixelWidthHeightRatio).roundToInt()
    return if (rotationDegrees == 90 || rotationDegrees == 270) {
        VideoDisplaySize(storedHeight, width)
    } else {
        VideoDisplaySize(width, storedHeight)
    }
}
