import com.vanniktech.maven.publish.JavadocJar
import com.vanniktech.maven.publish.KotlinJvm
import com.vanniktech.maven.publish.SourcesJar
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm")
    kotlin("plugin.compose")
    id("org.jetbrains.compose")
    `mpp-lib-targets`
    id(libs.plugins.vanniktech.mavenPublish.get().pluginId)
}

description = "Optional Nucleus TAO desktop surface for the MediaMP MPV backend"

dependencies {
    api(projects.mediampMpv)
    implementation("dev.nucleusframework:nucleus.decorated-window-tao:${libs.versions.nucleus.get()}")
    implementation(libs.compose.foundation)
    testImplementation(kotlin("test"))
    testImplementation("dev.nucleusframework:nucleus.nucleus-application:${libs.versions.nucleus.get()}")
    testRuntimeOnly(compose.desktop.currentOs)
}

kotlin {
    explicitApi()
    compilerOptions.jvmTarget.set(JvmTarget.JVM_17)
}
java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

mavenPublishing {
    configure(KotlinJvm(JavadocJar.Empty(), SourcesJar.Sources()))
    publishToMavenCentral()
    signAllPublicationsIfEnabled(project)
    configurePom(project)
}

tasks.register<JavaExec>("taoSmoke") {
    group = "verification"
    description = "Run a real TAO window with a local test video, seek, resize and screenshot assertions"
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass.set("org.openani.mediamp.mpv.tao.TaoSmokeMainKt")
    jvmArgs("--enable-native-access=ALL-UNNAMED")
    if (getOs() == Os.MacOS) jvmArgs("-XstartOnFirstThread")
    systemProperty("mediamp.tao.smoke.video", providers.gradleProperty("mediamp.tao.smoke.video").orNull ?: "")
    systemProperty("mediamp.tao.smoke.output", providers.gradleProperty("mediamp.tao.smoke.output").orNull ?: layout.buildDirectory.dir("tao-smoke").get().asFile.absolutePath)
    providers.gradleProperty("mediamp.tao.smoke.native.dir").orNull?.let {
        systemProperty("mediamp.tao.smoke.native.dir", it)
    }
}
