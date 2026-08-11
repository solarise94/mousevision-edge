import org.gradle.api.DefaultTask
import org.gradle.api.tasks.Input
import org.gradle.api.tasks.OutputDirectory
import org.gradle.api.tasks.TaskAction
import org.gradle.kotlin.dsl.register
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}

// 打包 app 运行配置（构建期 -P 注入，见 README）：
// - MICE_API_BASE：API 服务器地址，默认生产地址；
// - MICE_SYNC_TOKEN：上报同步令牌，默认空（由主代理提供，留好通道即可）。
// 本地文件注入：rootProject/sync.properties（gitignored）里同名属性优先于默认值，
// -P 命令行属性优先于文件。
val syncPropsFile = rootProject.file("sync.properties")
val syncProps = Properties().apply {
    if (syncPropsFile.exists()) syncPropsFile.inputStream().use { load(it) }
}
val miceApiBase: String =
    providers.gradleProperty("MICE_API_BASE")
        .getOrElse(syncProps.getProperty("MICE_API_BASE") ?: "https://weight.pingoodmice.top:16206")
val miceSyncToken: String =
    providers.gradleProperty("MICE_SYNC_TOKEN")
        .getOrElse(syncProps.getProperty("MICE_SYNC_TOKEN") ?: "")
// 共享令牌（local 公众版「共享数据以改善应用」上传通道）。
// 仅 local flavor 注入 config.js；cloud 版写空串（无共享通道）。
val miceShareToken: String =
    providers.gradleProperty("MICE_SHARE_TOKEN")
        .getOrElse(syncProps.getProperty("MICE_SHARE_TOKEN") ?: "")

android {
    namespace = "com.pingoodmice.miceautomatic"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.pingoodmice.miceautomatic"
        minSdk = 26
        targetSdk = 35
        versionCode = 6
        versionName = "0.3.3"

        // 应用名由各 flavor 覆盖（cloud=「小鼠称重」，local=「小鼠称重·本地版」）。
        resValue("string", "app_name", "小鼠称重")

        buildConfigField(
            "String",
            "MICE_WEB_URL",
            "\"https://weight.pingoodmice.top:16206/mobile\"",
        )
        // 实验室测试期：cloud（实验/内测）版保持 dev 模式采集训练数据；
        // local（公众本地版）关闭（buildConfigField 在 flavor 中覆盖为 false）。
        buildConfigField("boolean", "MICE_DEV_MODE", "true")
    }

    // 单维 flavor：edition = cloud（现状）/ local（公众本地版）。
    flavorDimensions += "edition"
    productFlavors {
        create("cloud") {
            dimension = "edition"
            // 现状：applicationId 不变，dev 模式保持 true。
        }
        create("local") {
            dimension = "edition"
            applicationIdSuffix = ".local"
            resValue("string", "app_name", "小鼠称重·本地版")
            // 公众版不采集训练数据。
            buildConfigField("boolean", "MICE_DEV_MODE", "false")
        }
    }

    // H5 资产由 syncH5Assets 从 ../ui/static 拷到 generated/assets/www
    // （不经 src/main/assets，避免与手工资产混淆）。
    sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("generated/assets"))

    buildFeatures {
        buildConfig = true
    }

    signingConfigs {
        create("release") {
            if (keystorePropsFile.exists()) {
                storeFile = file(keystoreProps["storeFile"] as String)
                storePassword = keystoreProps["storePassword"] as String
                keyAlias = keystoreProps["keyAlias"] as String
                keyPassword = keystoreProps["keyPassword"] as String
            }
        }
    }

    buildTypes {
        release {
            if (keystorePropsFile.exists()) {
                signingConfig = signingConfigs.getByName("release")
            }
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        // 实验室分发包不跑 release lint（其依赖下载在国内网络不稳）
        checkReleaseBuilds = false
        abortOnError = false
    }
}

// ---------------------------------------------------------------------------
// H5 资产打包（共用）+ config.js 按 flavor 生成
// 源：<repo>/ui/static（H5 本体 + mobile.html 入口）；产物：assets/www/
//
// config.js 必须按 flavor 区分（edition / token），所以不能放在 main sourceSet。
// 采用「每 flavor 一个 assets srcDir + 覆盖文件」方案：
// - main sourceSet 的 assets 由 syncH5Assets 拷入 build/generated/assets（不含 config.js）；
// - 每个 flavor 额外挂一个 build/generated/assets-<flavor> assets srcDir（优先级高于 main），
//   由对应 generate*Config 任务写入其专属 config.js（assets/www/config.js），覆盖 main 里
//   同路径资源。
//
// 不用 androidComponents.addGeneratedSourceDirectory：它会把输出目录固定在
// build/generated/assets/<taskName>（落在 main assets srcDir 扫描范围内），导致各 variant
// 的 config 互相污染（见构建期 cross-variant implicit-dependency 报错）。
// ---------------------------------------------------------------------------

val staticDir = rootProject.file("../ui/static")
val generatedAssetsDir = layout.buildDirectory.dir("generated/assets/www")

tasks.register<Copy>("syncH5Assets") {
    from(staticDir)
    into(generatedAssetsDir)
    // config.js 为构建期按 flavor 生成，避免被源文件覆盖。
    exclude("config.js")
    inputs.property("apiBase", miceApiBase)
    inputs.property("token", miceSyncToken)
}

// 每个 flavor 生成其专属 config.js（edition / token 因 flavor 而异）。
abstract class GenerateAppConfig : DefaultTask() {
    @get:Input
    abstract var apiBase: String

    @get:Input
    abstract var syncToken: String

    @get:Input
    abstract var shareToken: String

    @get:Input
    abstract var edition: String

    @get:OutputDirectory
    abstract val outputDir: org.gradle.api.file.DirectoryProperty

    @TaskAction
    fun generate() {
        val file = outputDir.get().file("www/config.js").asFile
        file.parentFile.mkdirs()
        // JS 字符串安全转义（token 可能含引号/反斜杠）。
        fun jsStr(v: String): String = v.replace("\\", "\\\\").replace("'", "\\'")
        val appOrigin = "https://app.miceautomatic.local"
        file.writeText(
            "// 构建期生成：app 独立运行配置（勿手改）\n" +
                "window.MV_CONFIG = { apiBase: '" + jsStr(apiBase) +
                "', token: '" + jsStr(syncToken) +
                "', shareToken: '" + jsStr(shareToken) +
                "', edition: '" + edition +
                "', appOrigin: '" + appOrigin + "' };\n",
        )
    }
}

// 每个 flavor 专属 assets 根目录（优先级高于 main，assets/www/config.js 覆盖 main 同路径）。
listOf("cloud", "local").forEach { flavor ->
    val cfgDir = layout.buildDirectory.dir("generated/assets-$flavor")
    android.sourceSets[flavor].assets.srcDir(cfgDir)
    val configTask = tasks.register<GenerateAppConfig>("generateAppConfig$flavor") {
        apiBase = miceApiBase
        // local（公众本地版）不携带同步令牌：纯本地不上传。
        syncToken = if (flavor == "local") "" else miceSyncToken
        // 共享通道仅 local 版注入（云版无共享上传）。
        shareToken = if (flavor == "local") miceShareToken else ""
        edition = flavor
        outputDir.set(cfgDir)
    }
    tasks.named("preBuild") { dependsOn(configTask) }
}

tasks.named("preBuild") {
    dependsOn("syncH5Assets")
}

dependencies {
    // org.json 的 JVM 实现：Android SDK stub 无法在普通 JVM 单测中执行，
    // 用官方 org.json 实现让 ScaleProfileRegistry.parseProfiles 在 testDebugUnitTest 中可跑。
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20231013")
}
