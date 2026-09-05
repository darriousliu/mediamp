/*
 * Copyright (C) 2024-2026 OpenAni and contributors.
 *
 * Use of this source code is governed by the Apache License version 2 license, which can be found at the following link.
 *
 * https://github.com/open-ani/mediamp/blob/main/LICENSE
 */

package org.openani.mediamp.exoplayer.internal

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class ExoVideoDisplaySizeTest {

    @Test
    fun `square pixels pass the stored size through`() {
        assertEquals(
            VideoDisplaySize(1920, 1080),
            displaySizeOrNull(1920, 1080, pixelWidthHeightRatio = 1f, rotationDegrees = 0),
        )
    }

    @Test
    fun `anamorphic ratio widens the stored width`() {
        // 720x576 PAL stored at 16:9 display aspect: PAR 1.4587 -> 1050x576.
        assertEquals(
            VideoDisplaySize(1050, 576),
            displaySizeOrNull(720, 576, pixelWidthHeightRatio = 1.4587f, rotationDegrees = 0),
        )
    }

    @Test
    fun `quarter turns swap the axes`() {
        for (rotation in listOf(90, 270)) {
            assertEquals(
                VideoDisplaySize(1080, 1920),
                displaySizeOrNull(1920, 1080, pixelWidthHeightRatio = 1f, rotationDegrees = rotation),
                "rotation=$rotation",
            )
        }
    }

    @Test
    fun `half turns keep the axes`() {
        assertEquals(
            VideoDisplaySize(1920, 1080),
            displaySizeOrNull(1920, 1080, pixelWidthHeightRatio = 1f, rotationDegrees = 180),
        )
    }

    @Test
    fun `a quarter turn scales the display height by the ratio`() {
        // The ratio applies to the stored width, which becomes the display height after the turn.
        assertEquals(
            VideoDisplaySize(576, 1050),
            displaySizeOrNull(720, 576, pixelWidthHeightRatio = 1.4587f, rotationDegrees = 90),
        )
    }

    @Test
    fun `an unknown stored size has no display size`() {
        assertNull(displaySizeOrNull(0, 1080, pixelWidthHeightRatio = 1f, rotationDegrees = 0))
        assertNull(displaySizeOrNull(1920, 0, pixelWidthHeightRatio = 1f, rotationDegrees = 0))
        assertNull(displaySizeOrNull(-1, -1, pixelWidthHeightRatio = 1f, rotationDegrees = 0))
    }
}
