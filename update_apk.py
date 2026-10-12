#!/usr/bin/env python3
"""Atualiza APKs antigos para instalarem e rodarem em versões novas do Android.

Coloque os arquivos (.apk, .xapk, .apks, .apkm) na pasta ./apks e rode:
    python3 update_apk.py

Se houver um celular conectado por USB (com Depuração USB ativa), os apps
já são instalados nele. Também aceita arquivos específicos:
    python3 update_apk.py jogo.xapk outro.apk
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
INPUT_DIR = ROOT / "apks"
OUTPUT_DIR = ROOT / "atualizados"

SIGNER_JAR = TOOLS / "uber-apk-signer.jar"
EDITOR_JAR = TOOLS / "APKEditor.jar"
DOWNLOADS = {
    SIGNER_JAR: "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar",
    EDITOR_JAR: "https://github.com/REAndroid/APKEditor/releases/download/V1.4.9/APKEditor-1.4.9.jar",
}
BREW_PACKAGES = {
    "java": ["openjdk@17"],
    "apktool": ["apktool"],
    "adb": ["--cask", "android-platform-tools"],
}

BUNDLE_EXTS = {".xapk", ".apks", ".apkm"}
INPUT_EXTS = BUNDLE_EXTS | {".apk"}

ANDROID_NS = "http://schemas.android.com/apk/res/android"
A = "{%s}" % ANDROID_NS

# Android 14 bloqueia instalação de apps com targetSdk < 23 e o Android 15 com < 24.
DEFAULT_TARGET = 24

DANGEROUS_PERMISSIONS = {
    "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "CAMERA", "RECORD_AUDIO",
    "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "READ_CONTACTS",
    "WRITE_CONTACTS", "GET_ACCOUNTS", "READ_PHONE_STATE", "CALL_PHONE",
    "READ_SMS", "SEND_SMS", "RECEIVE_SMS", "READ_CALENDAR", "WRITE_CALENDAR",
    "BODY_SENSORS", "READ_CALL_LOG", "WRITE_CALL_LOG",
}
ABI_64 = {"arm64-v8a", "x86_64"}


def log(msg=""):
    print(msg, flush=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def run_or_fail(cmd, step):
    res = run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"falha ao {step}:\n" + (res.stderr or res.stdout)[-2000:])
    return res


def ask(question):
    if not sys.stdin.isatty():
        return False
    return input(f"  {question} [s/N] ").strip().lower() in ("s", "sim", "y")


# ---------------------------------------------------------------- ferramentas

def ensure_tools():
    for cmd, pkg in BREW_PACKAGES.items():
        if shutil.which(cmd):
            continue
        if not shutil.which("brew"):
            sys.exit(f"Falta '{cmd}'. Instale o Homebrew (https://brew.sh) e rode de novo.")
        log(f"Instalando {cmd}...")
        subprocess.run(["brew", "install", *pkg], check=True)

    TOOLS.mkdir(exist_ok=True)
    for path, url in DOWNLOADS.items():
        if not path.exists():
            log(f"Baixando {path.name}...")
            urllib.request.urlretrieve(url, path)


def connected_device():
    lines = run(["adb", "devices"]).stdout.splitlines()[1:]
    return any(l.strip().endswith("\tdevice") for l in lines)


# ---------------------------------------------------------------- apktool.yml

def read_sdk_info(yml_text):
    def get(key):
        m = re.search(rf"^\s+{key}:\s*'?(\d+)'?\s*$", yml_text, re.M)
        return int(m.group(1)) if m else None
    return get("minSdkVersion"), get("targetSdkVersion")


def set_target_in_yml(yml_text, target):
    pattern = re.compile(r"^(\s+targetSdkVersion:\s*)'?\d+'?\s*$", re.M)
    if pattern.search(yml_text):
        return pattern.sub(rf"\g<1>{target}", yml_text)
    if re.search(r"^sdkInfo:\s*$", yml_text, re.M):
        return re.sub(r"^(sdkInfo:\s*)$", rf"\1\n  targetSdkVersion: {target}",
                      yml_text, count=1, flags=re.M)
    return yml_text + f"\nsdkInfo:\n  targetSdkVersion: {target}\n"


def compress_native_libs_in_yml(yml_text):
    # Com extractNativeLibs=true as .so podem ir comprimidas, evitando
    # problemas de alinhamento (4 KB / 16 KB) em aparelhos novos.
    return re.sub(r"^- so\s*\n", "", yml_text, flags=re.M)


# ---------------------------------------------------------------- manifesto

def load_manifest(path):
    for _, (prefix, uri) in ET.iterparse(path, events=["start-ns"]):
        ET.register_namespace(prefix, uri)
    return ET.parse(path)


def patch_manifest(tree, target, changes):
    root = tree.getroot()
    uses_sdk = root.find("uses-sdk")
    if uses_sdk is not None and uses_sdk.get(A + "targetSdkVersion"):
        uses_sdk.set(A + "targetSdkVersion", str(target))

    app = root.find("application")
    if app is None:
        return

    def set_if_missing(name, why):
        if app.get(A + name) is None:
            app.set(A + name, "true")
            changes.append(why)

    if app.get(A + "extractNativeLibs") != "true":
        app.set(A + "extractNativeLibs", "true")
        changes.append("bibliotecas nativas extraídas na instalação")

    if app.attrib.pop(A + "testOnly", None) is not None:
        changes.append("removida marcação de app de teste")

    if target >= 28:
        set_if_missing("usesCleartextTraffic", "permite conexões HTTP sem criptografia")
        libs = {el.get(A + "name") for el in app.findall("uses-library")}
        if "org.apache.http.legacy" not in libs:
            lib = ET.SubElement(app, "uses-library")
            lib.set(A + "name", "org.apache.http.legacy")
            lib.set(A + "required", "false")
            changes.append("adicionada biblioteca HTTP antiga do Apache")

    if target >= 29:
        set_if_missing("requestLegacyExternalStorage", "acesso antigo ao armazenamento")

    if target >= 31:
        count = 0
        for tag in ("activity", "activity-alias", "service", "receiver"):
            for el in app.findall(tag):
                if el.find("intent-filter") is not None and el.get(A + "exported") is None:
                    el.set(A + "exported", "true")
                    count += 1
        if count:
            changes.append(f"android:exported adicionado em {count} componente(s)")


def dangerous_permissions(tree):
    names = {(el.get(A + "name") or "").rsplit(".", 1)[-1]
             for el in tree.getroot().findall("uses-permission")}
    return sorted(names & DANGEROUS_PERMISSIONS)


def native_abis(decoded_dir):
    lib = decoded_dir / "lib"
    if not lib.is_dir():
        return []
    return sorted(p.name for p in lib.iterdir() if p.is_dir() and any(p.glob("*.so")))


def warnings_for(cur_target, new_target, abis, perms):
    warns = []
    if abis and not set(abis) & ABI_64:
        warns.append(
            f"Só tem código nativo 32 bits ({', '.join(abis)}). Celulares só 64 bits "
            "(ex.: Pixel 7 ou mais novo e vários aparelhos de 2024+) não rodam este app; "
            "isso não tem como corrigir.")
    if (cur_target or 0) < 23 <= new_target and perms:
        warns.append(
            "Se o app travar, ative as permissões em Configurações > Apps > Permissões: "
            + ", ".join(perms) + ". (Instalando pelo USB elas já vêm ativadas.)")
    if new_target >= 31:
        warns.append("targetSdk >= 31 costuma quebrar apps antigos; prefira 24 a 30.")
    return warns


# ---------------------------------------------------------------- pacotes divididos

def unpack_bundle(bundle, tmp):
    """Junta .xapk/.apks/.apkm em um APK único e separa os arquivos OBB."""
    merged = tmp / "merged.apk"
    run_or_fail(["java", "-jar", str(EDITOR_JAR), "m", "-f",
                 "-i", str(bundle), "-o", str(merged)], "juntar o pacote")
    obbs = []
    with zipfile.ZipFile(bundle) as z:
        for name in z.namelist():
            if name.lower().endswith(".obb"):
                dest = tmp / "obb" / Path(name).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                obbs.append(dest)
    return merged, obbs


# ---------------------------------------------------------------- pipeline

def process(source, args, device):
    log(f"\n=== {source.name} ===")
    with tempfile.TemporaryDirectory(prefix="update-apk-") as tmp:
        tmp = Path(tmp)
        apk, obbs = source, []
        if source.suffix.lower() in BUNDLE_EXTS:
            apk, obbs = unpack_bundle(source, tmp)
            log(f"  Pacote dividido juntado em um APK único" +
                (f" ({len(obbs)} arquivo(s) OBB)" if obbs else ""))

        decoded = tmp / "src"
        run_or_fail(["apktool", "d", "-f", "-s", "-o", str(decoded), str(apk)], "descompilar")

        yml_path = decoded / "apktool.yml"
        yml = yml_path.read_text()
        min_sdk, cur_target = read_sdk_info(yml)
        manifest_path = decoded / "AndroidManifest.xml"
        tree = load_manifest(manifest_path)
        package = tree.getroot().get("package")
        abis = native_abis(decoded)
        perms = dangerous_permissions(tree)
        new_target = max(cur_target or min_sdk or 1, args.target)

        log(f"  {package} | targetSdk {cur_target} | "
            + (f"nativo: {', '.join(abis)}" if abis else "sem código nativo"))

        if args.analisar:
            if (cur_target or 0) < DEFAULT_TARGET:
                log("  -> Precisa de atualização para instalar no Android 14 ou mais novo.")
            else:
                log("  -> Já instala em Android novo sem modificações.")
            for w in warnings_for(cur_target, new_target, abis, perms):
                log(f"  ! {w}")
            return

        changes = []
        if new_target != cur_target:
            yml = set_target_in_yml(yml, new_target)
            changes.append(f"targetSdk {cur_target} -> {new_target}")
        yml_path.write_text(compress_native_libs_in_yml(yml))
        patch_manifest(tree, new_target, changes)
        tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
        for c in changes:
            log(f"  - {c}")

        unsigned = tmp / "unsigned.apk"
        run_or_fail(["apktool", "b", "-o", str(unsigned), str(decoded)], "recompilar")
        run_or_fail(["java", "-jar", str(SIGNER_JAR), "-a", str(unsigned),
                     "-o", str(tmp / "signed"), "--allowResign"], "assinar")
        signed = next((tmp / "signed").glob("*.apk"))

        OUTPUT_DIR.mkdir(exist_ok=True)
        final = OUTPUT_DIR / f"{source.stem}-atualizado.apk"
        shutil.move(signed, final)
        log(f"  Pronto: atualizados/{final.name}")

        if obbs:
            obb_dir = OUTPUT_DIR / f"{source.stem}-OBB" / package
            obb_dir.mkdir(parents=True, exist_ok=True)
            for obb in obbs:
                shutil.copy2(obb, obb_dir / obb.name)
            log(f"  OBB: copie a pasta atualizados/{obb_dir.parent.name}/{package} "
                "para Android/obb/ no celular")

        for w in warnings_for(cur_target, new_target, abis, perms):
            log(f"  ! {w}")

        if device:
            install(final, package, obbs)


def install(apk, package, obbs):
    def adb_install():
        res = run(["adb", "install", "-r", "-g", str(apk)])
        return (res.stdout + res.stderr).strip()

    out = adb_install()
    if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:
        log("  O celular já tem outra versão deste app, com assinatura diferente.")
        if not ask("Desinstalar a versão atual (os dados do app serão apagados)?"):
            log("  Instalação pulada.")
            return
        run(["adb", "uninstall", package])
        out = adb_install()

    if "Success" not in out:
        log(f"  ! Não instalou: {out.splitlines()[-1] if out else 'erro desconhecido'}")
        return
    for obb in obbs:
        run(["adb", "push", str(obb), f"/sdcard/Android/obb/{package}/{obb.name}"])
    log("  Instalado no celular" + (" (com OBB)" if obbs else "") + ".")


def collect_inputs(paths):
    found = []
    for p in map(Path, paths or [INPUT_DIR]):
        if p.is_dir():
            found += sorted(f for f in p.iterdir() if f.suffix.lower() in INPUT_EXTS)
        elif p.exists():
            found.append(p)
        else:
            log(f"! {p} não encontrado")
    return found


def main():
    parser = argparse.ArgumentParser(description="Atualiza APKs antigos para Android moderno.")
    parser.add_argument("entradas", nargs="*",
                        help="arquivos .apk/.xapk/.apks/.apkm ou pastas (padrão: ./apks)")
    parser.add_argument("--analisar", action="store_true",
                        help="só mostra o diagnóstico, sem gerar nada")
    parser.add_argument("--nao-instalar", action="store_true",
                        help="não instala no celular mesmo se houver um conectado")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET,
                        help=f"targetSdk mínimo (padrão: {DEFAULT_TARGET})")
    args = parser.parse_args()

    INPUT_DIR.mkdir(exist_ok=True)
    files = collect_inputs(args.entradas)
    if not files:
        log(f"Nenhum app encontrado. Coloque arquivos .apk ou .xapk na pasta:\n  {INPUT_DIR}")
        if sys.platform == "darwin":
            subprocess.run(["open", str(INPUT_DIR)])
        return

    ensure_tools()
    device = not args.analisar and not args.nao_instalar and connected_device()
    if device:
        log("Celular conectado: os apps serão instalados nele.")

    failed = []
    for f in files:
        try:
            process(f, args, device)
        except Exception as e:
            log(f"  ERRO: {e}")
            failed.append(f.name)

    log(f"\nConcluído: {len(files) - len(failed)} ok, {len(failed)} com erro.")
    if failed:
        log("Falharam: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
