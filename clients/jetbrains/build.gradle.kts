plugins {
    id("java")
    id("org.jetbrains.kotlin.jvm") version "2.1.0"
    id("org.jetbrains.intellij.platform") version "2.2.1"
}

group = "sh.igniteember"

// ``pyproject.toml`` is the single source of truth for the version
// across the Python package + every plugin/client that ships with
// it. The plugin's own version, the bundled ``ember-version.properties``
// resource, AND the ``ignite-ember`` pip pin all derive from it —
// bumping one place flows everywhere.
val pyprojectFile = rootProject.file("../../pyproject.toml")
val pyprojectVersion: String = run {
    val line = pyprojectFile.readLines().firstOrNull { it.trim().startsWith("version") }
        ?: error("version not found in pyproject.toml")
    Regex("""version\s*=\s*"([^"]+)"""").find(line)?.groupValues?.get(1)
        ?: error("could not parse version from: $line")
}
// ``-PpluginSubversion=N`` on the gradle command line ships this
// plugin build as ``<pyprojectVersion>.N`` while keeping the pinned
// ``ignite-ember`` pip version at ``<pyprojectVersion>`` (that
// package is what the plugin bootstraps on first launch; bumping the
// suffix here would send the runtime looking for a PyPI release
// that doesn't exist). Used for JetBrains-only hotfixes between
// full-repo releases — pick up a plugin fix without cutting a new
// Python package.
val pluginSubversion: String = (findProperty("pluginSubversion") as? String)
    ?.trim().orEmpty()
val pluginVersion: String =
    if (pluginSubversion.isEmpty()) pyprojectVersion else "$pyprojectVersion.$pluginSubversion"
version = pluginVersion

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        // Build against IntelliJ IDEA Community 2024.2.4 — the lowest
        // platform version the plugin supports. The resulting plugin
        // jar runs in any 2024.2+ IDE (IntelliJ, PyCharm, WebStorm,
        // RubyMine, etc) because they share the same IntelliJ
        // Platform — the user installs the built zip into their own
        // PyCharm via Settings → Plugins → ⚙ → Install from Disk.
        intellijIdeaCommunity("2024.2.4")
        // Plain JUnit 5 is all we need — our tests cover pure
        // helpers (path resolution, JSON escaping) without spinning
        // up the platform. A heavier ``testFramework(Platform)``
        // dependency would unlock ``BasePlatformTestCase`` but
        // would add ~10 s per test for IDE bootstrap; not worth it
        // for unit-level coverage.
    }
    testImplementation("org.junit.jupiter:junit-jupiter:5.11.4")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher:1.11.4")
}

tasks.test {
    useJUnitPlatform()
}

kotlin {
    // 21 matches the JBR shipped with 2024.2+. Avoids needing a
    // separate JDK download — we point JAVA_HOME at PyCharm's
    // bundled JBR (``/Applications/PyCharm.app/Contents/jbr``).
    jvmToolchain(21)
}

// The shared web UI is staged into ``src/main/resources/webui`` by
// ``scripts/build-clients.sh`` at the repo root (which runs the web
// build and copies into both VSCode + JetBrains trees). gradle then
// picks the contents up via the standard resource pipeline. Run the
// script before ``gradle buildPlugin``/``gradle runIde`` whenever the
// web UI changes.

// ── Resource generator for the runtime version pin ──────────────
//
// ``pyprojectVersion`` (parsed at the top of this file) is the SoT.
// The Kotlin runtime needs ``IGNITE_EMBER_VERSION`` at load time;
// rather than hardcoding it in source, we generate a properties
// resource at build time and the plugin reads it via classloader.
// Values are captured into local vals so the task body avoids
// ``project`` references and stays configuration-cache-safe.
val pluginVersionString: String = project.version.toString()
val versionResourceDir = layout.buildDirectory.dir("generated/resources/ember-version")

val generateEmberVersion by tasks.registering {
    val outDirProvider = versionResourceDir
    val captured = pyprojectVersion
    val capturedPlugin = pluginVersionString
    inputs.file(pyprojectFile)
    inputs.property("ignite-ember-version", captured)
    inputs.property("plugin-version", capturedPlugin)
    outputs.dir(outDirProvider)
    doLast {
        val target = outDirProvider.get().asFile.resolve("META-INF/ember-version.properties")
        target.parentFile.mkdirs()
        target.writeText(
            "ignite-ember-version=$captured\n" +
                "plugin-version=$capturedPlugin\n"
        )
    }
}

sourceSets["main"].resources.srcDir(generateEmberVersion)
tasks.named("processResources") { dependsOn(generateEmberVersion) }

intellijPlatform {
    pluginConfiguration {
        name = "igni"
        ideaVersion {
            sinceBuild = "242"
            // Marketplace prefers an explicit upper bound. Bumped in
            // lockstep with each verified-against IntelliJ Platform
            // release; the verifier task below catches breakage
            // before we publish a new bound.
            untilBuild = "253.*"
        }
    }
    pluginVerification {
        // ``./gradlew verifyPlugin`` runs the IntelliJ Plugin
        // Verifier against the listed IDE builds and catches binary
        // incompatibilities BEFORE Marketplace does. Cheap to run
        // locally and an obvious thing to add to CI.
        ides {
            recommended()
        }
    }
    publishing {
        // Marketplace upload token. Sourced from the
        // ``ORG_GRADLE_PROJECT_jbToken`` env var in CI (set by the
        // ``JETBRAINS_MARKETPLACE_TOKEN`` GitHub secret); on a
        // developer's machine the gradle.properties file in the
        // user's ~/.gradle dir is the conventional place if you
        // ever want to publish locally.
        //
        // Generate one at
        // https://plugins.jetbrains.com/author/me/tokens — scope
        // "Upload Plugin" for the ``igni`` plugin. First-time
        // publish triggers JetBrains' moderation review (1-10
        // business days); subsequent updates pass through within
        // minutes.
        token = providers.gradleProperty("jbToken").orNull
    }
}
