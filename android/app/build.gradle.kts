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

android {
    namespace = "com.pingoodmice.miceautomatic"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.pingoodmice.miceautomatic"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "0.3.0"

        buildConfigField(
            "String",
            "MICE_WEB_URL",
            "\"https://weight.pingoodmice.top:16206/mobile\"",
        )
        // 实验室测试期：dev 版每条记录附带读数时间序列用于模型训练；正式发布改 false
        buildConfigField("boolean", "MICE_DEV_MODE", "true")
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
// H5 资产打包 + config.js 生成
// 源：<repo>/ui/static（H5 本体 + mobile.html 入口）；产物：assets/www/
// ---------------------------------------------------------------------------

val staticDir = rootProject.file("../ui/static")
val generatedAssetsDir = layout.buildDirectory.dir("generated/assets/www")

tasks.register<Copy>("syncH5Assets") {
    from(staticDir)
    into(generatedAssetsDir)
    // config.js 为构建期生成，避免被源文件覆盖。
    exclude("config.js")
    inputs.property("apiBase", miceApiBase)
    inputs.property("token", miceSyncToken)
}

tasks.register("generateAppConfig") {
    val outFile = generatedAssetsDir.map { it.file("config.js") }
    inputs.property("apiBase", miceApiBase)
    inputs.property("token", miceSyncToken)
    outputs.file(outFile)
    doLast {
        val file = outFile.get().asFile
        file.parentFile.mkdirs()
        // JS 字符串安全转义（token 可能含引号/反斜杠）。
        fun jsStr(v: String): String = v.replace("\\", "\\\\").replace("'", "\\'")
        val appOrigin = "https://app.miceautomatic.local"
        file.writeText(
            "// 构建期生成：app 独立运行配置（勿手改）\n" +
                "window.MV_CONFIG = { apiBase: '" + jsStr(miceApiBase) +
                "', token: '" + jsStr(miceSyncToken) +
                "', appOrigin: '" + appOrigin + "' };\n",
        )
    }
}

tasks.named("generateAppConfig") {
    dependsOn("syncH5Assets")
}

tasks.named("preBuild") {
    dependsOn("generateAppConfig")
}
