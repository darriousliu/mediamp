/*
 * Copyright (C) 2024-2025 OpenAni and contributors.
 *
 * Use of this source code is governed by the Apache License version 2 license, which can be found at the following link.
 *
 * https://github.com/open-ani/mediamp/blob/main/LICENSE
 */

import org.jetbrains.kotlin.gradle.dsl.KotlinMultiplatformExtension
import org.jetbrains.kotlin.gradle.dsl.kotlinExtension
import groovy.json.JsonSlurper
import org.gradle.api.publish.PublishingExtension
import org.gradle.api.publish.maven.MavenPublication

buildscript {
    repositories {
        gradlePluginPortal()
        mavenCentral()
        google()
        maven("https://maven.pkg.jetbrains.space/public/p/compose/dev")
    }
}

plugins {
    id(libs.plugins.kotlin.multiplatform.get().pluginId) apply false
    id(libs.plugins.kotlin.jvm.get().pluginId) apply false
    alias(libs.plugins.kotlin.plugin.serialization) apply false
    id(libs.plugins.kotlin.plugin.compose.get().pluginId) apply false
//    id("org.jetbrains.kotlinx.atomicfu") version libs.versions.atomicfu apply false
//    alias(libs.plugins.kotlinx.atomicfu) apply false
    id(libs.plugins.compose.get().pluginId) apply false
    id(libs.plugins.android.kotlin.multipaltform.library.get().pluginId) apply false
    id(libs.plugins.android.library.get().pluginId) apply false
    id(libs.plugins.android.application.get().pluginId) apply false
    id(libs.plugins.vanniktech.mavenPublish.get().pluginId) apply false
    idea
}

allprojects {
    group = providers.gradleProperty("maven.group").getOrElse("org.openani.mediamp")
    version = properties["version.name"].toString()

    repositories {
        mavenCentral()
        google()
        maven("https://maven.pkg.jetbrains.space/public/p/compose/dev")
        maven("https://androidx.dev/storage/compose-compiler/repository/")
    }

    afterEvaluate {
        (runCatching { kotlinExtension }.getOrNull() as? KotlinMultiplatformExtension)?.apply {
            compilerOptions {
                optIn.add("kotlin.ExperimentalSubclassOptIn") // Workaround for IDE bug. This is already stable in Kotlin 2.1.0
            }
        }
    }
}

extensions.findByName("buildScan")?.withGroovyBuilder {
    setProperty("termsOfServiceUrl", "https://gradle.com/terms-of-service")
    setProperty("termsOfServiceAgree", "yes")
}

idea {
    module {
        excludeDirs.add(file(".kotlin"))
    }
}

// The same checked-in inventory is consumed by the release verifier. Missing
// native targets must fail here, before a partial release can reach a registry.
@Suppress("UNCHECKED_CAST")
val fullReleaseInventory = JsonSlurper().parse(file("scripts/tao-publications.json")) as Map<String, Any?>
val fullReleaseExpected = buildSet {
    (fullReleaseInventory.getValue("multiplatform") as Map<*, *>).forEach { (module, targets) ->
        add(module.toString())
        (targets as List<*>).forEach { add("$module-$it") }
    }
    (fullReleaseInventory.getValue("standalone") as Map<*, *>).keys.forEach { add(it.toString()) }
    (fullReleaseInventory.getValue("nativeFamilies") as List<*>).forEach { family ->
        add("mediamp-$family-runtime")
        (fullReleaseInventory.getValue("nativePlatforms") as List<*>).forEach {
            add("mediamp-$family-runtime-$it")
        }
    }
    add(fullReleaseInventory.getValue("xcframework").toString())
}
val stageTaoFullRelease = tasks.register("stageTaoFullRelease") {
    group = "publishing"
    description = "Stage all 65 MediaMP publications, including mobile and every native runtime"
}
gradle.projectsEvaluated {
    val publications = subprojects.flatMap { module ->
        module.extensions.findByType<PublishingExtension>()?.publications
            ?.withType<MavenPublication>()?.map { module to it }.orEmpty()
    }
    val validate = tasks.register("validateTaoFullReleasePublications") {
        doLast {
            val actual = publications.map { it.second.artifactId }
            check(actual.size == actual.toSet().size) { "Duplicate Maven coordinates in release" }
            check(actual.toSet() == fullReleaseExpected) {
                "Incomplete release publications. Missing: ${fullReleaseExpected - actual.toSet()}; " +
                    "unexpected: ${actual.toSet() - fullReleaseExpected}"
            }
            check(publications.all { (_, publication) ->
                publication.groupId == fullReleaseInventory.getValue("group") && publication.version == project.version.toString()
            }) { "Every publication must use the fork namespace and the single release version" }
        }
    }
    stageTaoFullRelease.configure {
        dependsOn(validate)
        publications.forEach { (module, publication) ->
            val taskName = "publish${publication.name.replaceFirstChar { it.uppercase() }}PublicationToMavenLocal"
            val publishTask = module.tasks.named(taskName)
            publishTask.configure { mustRunAfter(validate) }
            dependsOn(publishTask)
        }
    }
}
