import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}

android {
    namespace = "com.pingoodmice.miceautomatic"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.pingoodmice.miceautomatic"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "0.2.0"

        buildConfigField(
            "String",
            "MICE_WEB_URL",
            "\"https://weight.pingoodmice.top:16206/mobile\"",
        )
        // 实验室测试期：dev 版每条记录附带读数时间序列用于模型训练；正式发布改 false
        buildConfigField("boolean", "MICE_DEV_MODE", "true")
    }

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
