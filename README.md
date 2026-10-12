# Update-apk

Faz APKs antigos (jogos e apps) instalarem e rodarem em Android novo.

## Como usar (macOS)

1. Coloque os arquivos `.apk`, `.xapk`, `.apks` ou `.apkm` na pasta `apks/`.
2. Dê dois cliques em **`Atualizar APKs.command`** (ou rode `python3 update_apk.py`).
3. Os apps atualizados aparecem em `atualizados/`.

Na primeira vez, o que faltar (Java, apktool, adb) é instalado via [Homebrew](https://brew.sh).

Se um celular estiver ligado por USB com **Depuração USB** ativa, os apps já são
instalados nele, com permissões e arquivos OBB.

Para só ver o diagnóstico, sem gerar nada: `python3 update_apk.py --analisar`.

## O que é corrigido

- `targetSdk` sobe para 24 (Android 14 recusa < 23 e Android 15 recusa < 24).
- Pacotes divididos (`.xapk`, `.apks`, `.apkm`) viram um APK único.
- Ajustes de manifesto para o novo target e nova assinatura (v1/v2/v3).

## O que não tem como corrigir

- **Jogos só 32 bits** não rodam em celulares só 64 bits (Pixel 7+ e vários
  aparelhos de 2024+). A ferramenta avisa quando é o caso.
- A assinatura muda: se a versão original estiver instalada, é preciso
  desinstalá-la antes (os dados do app são perdidos).
