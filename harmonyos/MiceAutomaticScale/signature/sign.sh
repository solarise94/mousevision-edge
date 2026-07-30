#!/bin/bash
# MiceAutomatic 鸿蒙 HAP 命令行签名脚本
#
# 用法：
#   bash signature/sign.sh [unsigned.hap] [signed.hap]
#   默认：
#     in  = entry/build/default/outputs/default/entry-default-unsigned.hap
#     out = entry/build/default/outputs/default/entry-default-signed.hap
#
# 流程：hvigor 只产出未签名 HAP（build-profile.json5 signingConfigs 留空），
# 本脚本用 hap-sign-tool sign-app -mode localSign 接受明文密码直接签名。
# 为何不放进 hvigor signingConfigs：hvigor 期望密文密码 + signature/material
# 加密目录（由 DevEco IDE 生成），纯 Command Line Tools 无对应加密生成工具。
#
# 签名材料（signature/，私钥与 profile 不入库）：
#   miceautomatic_debug.p12          本地 EC 密钥库（私钥，gitignore）
#   miceautomatic_debug.cer          AGC 调试证书链（可入库）
#   miceautomatic_debugDebug.p7b     AGC 调试 Profile（绑定设备 UDID，gitignore）
# 密码见 .deveco-env.sh（gitignore），勿提交真实口令。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLT="${CLT:-$PROJ_DIR/../../command-line-tools}"
JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home}"

IN="${1:-$PROJ_DIR/entry/build/default/outputs/default/entry-default-unsigned.hap}"
OUT="${2:-$PROJ_DIR/entry/build/default/outputs/default/entry-default-signed.hap}"

# 密钥库口令（调试，非生产）。可用环境变量覆盖。
KEY_PWD="${MICE_SIGN_PWD:-MiceAutomaticDebug2026HarmonyOS!}"

"$JAVA_HOME/bin/java" -jar "$CLT/sdk/default/openharmony/toolchains/lib/hap-sign-tool.jar" sign-app \
  -mode localSign \
  -keyAlias "miceautomatic_debug" \
  -keyPwd "$KEY_PWD" \
  -appCertFile "$SCRIPT_DIR/miceautomatic_debug.cer" \
  -profileFile "$SCRIPT_DIR/miceautomatic_debugDebug.p7b" \
  -profileSigned "1" \
  -inFile "$IN" \
  -inForm "zip" \
  -compatibleVersion "24" \
  -signAlg "SHA256withECDSA" \
  -keystoreFile "$SCRIPT_DIR/miceautomatic_debug.p12" \
  -keystorePwd "$KEY_PWD" \
  -outFile "$OUT" \
  -signCode "1"

echo "signed -> $OUT"
