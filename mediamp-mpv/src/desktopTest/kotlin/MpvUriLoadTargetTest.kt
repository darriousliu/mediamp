package org.openani.mediamp.mpv

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals

class MpvUriLoadTargetTest {
    @Test
    fun javaFileUriBecomesAnActualPathIncludingEscapedCharacters() {
        val file = File(System.getProperty("java.io.tmpdir"), "TAO 视频 #1 100%.mp4")
        assertEquals(file.absolutePath, mpvUriLoadTarget(file.toURI().toString()))
        assertEquals(file.absolutePath, mpvUriLoadTarget(file.toPath().toUri().toString()))
    }

    @Test
    fun otherSchemesAndRemoteFileAuthoritiesArePreserved() {
        for (uri in listOf("https://example.com/v%20a.mp4?token=%23a", "file://server/share/a.mp4", "content://media/1", "/tmp/video.mp4", "file:relative.mp4")) {
            assertEquals(uri, mpvUriLoadTarget(uri))
        }
    }
}
