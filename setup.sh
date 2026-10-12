#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

command -v java >/dev/null || brew install openjdk@17
command -v apktool >/dev/null || brew install apktool
command -v adb >/dev/null || brew install --cask android-platform-tools

mkdir -p tools apks atualizados
if [ ! -f tools/uber-apk-signer.jar ]; then
  curl -fL -o tools/uber-apk-signer.jar \
    https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar
fi

echo "Pronto. Coloque seus APKs em ./apks e rode: python3 update_apk.py"
