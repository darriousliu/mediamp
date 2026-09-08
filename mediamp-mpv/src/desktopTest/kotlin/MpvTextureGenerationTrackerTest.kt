package org.openani.mediamp.mpv

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class MpvTextureGenerationTrackerTest {
    @Test
    fun pendingReplacementCannotRetireTheStillVisibleImport() {
        val tracker = MpvTextureGenerationTracker()
        tracker.offer(1)
        tracker.attach(1)
        tracker.offer(2)
        assertFalse(tracker.canRetire(1))
        assertFalse(tracker.canRetire(2))
        tracker.detach(1)
        assertTrue(tracker.canRetire(1))
    }

    @Test
    fun skippedGenerationDoesNotStallFutureResizes() {
        val tracker = MpvTextureGenerationTracker()
        tracker.offer(3)
        assertTrue(tracker.canRetire(2)) // never offered or imported
        tracker.offer(4) // generation 3 never reached composition
        assertTrue(tracker.canRetire(3))
        assertFalse(tracker.canRetire(4))
    }

    @Test
    fun disposalReleasesAnOfferedGenerationAndWraparoundWorks() {
        val tracker = MpvTextureGenerationTracker()
        tracker.offer(65535)
        tracker.attach(65535)
        tracker.offer(0)
        assertFalse(tracker.canRetire(65535))
        tracker.detach(65535)
        assertTrue(tracker.canRetire(65535))
        tracker.attach(0)
        tracker.detach(0)
        assertTrue(tracker.canRetire(0))
    }
}
