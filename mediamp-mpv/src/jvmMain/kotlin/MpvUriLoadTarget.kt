package org.openani.mediamp.mpv

import java.io.File
import java.net.URI

/** Java File.toURI emits file:/..., which mpv treats as a literal filename. */
internal fun mpvUriLoadTarget(uri: String): String {
    if (!uri.startsWith("file:", ignoreCase = true)) return uri
    val parsed = runCatching { URI(uri) }.getOrNull() ?: return uri
    // Keep remote file authorities, opaque URIs and invalid escapes untouched. For a
    // local file use the decoded platform path (spaces/Unicode/# are real filename
    // characters, not URI syntax); File also handles Windows drive-letter paths.
    if (parsed.isOpaque || !parsed.rawAuthority.isNullOrEmpty()) return uri
    return runCatching { File(parsed).path }.getOrDefault(uri)
}
