#!/usr/bin/env python3
"""Atualiza APKs antigos para instalarem e rodarem em versões novas do Android.

Uso:
    python3 update_apk.py                    # processa todos os .apk em ./apks
    python3 update_apk.py jogo.apk outro.apk # processa arquivos específicos
    python3 update_apk.py --analisar jogo.apk
    python3 update_apk.py --target 28 jogo.apk
    python3 update_apk.py --instalar jogo.apk  # instala via adb no celular conectado
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIGNER_JAR = ROOT / "tools" / "uber-apk-signer.jar"
DEFAULT_INPUT = ROOT / "apks"
DEFAULT_OUTPUT = ROOT / "atualizados"

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


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def check_tools(need_adb=False):
    missing = []
    if not shutil.which("java"):
        missing.append("java (brew install openjdk@17)")
    if not shutil.which("apktool"):
        missing.append("apktool (brew install apktool)")
    if not SIGNER_JAR.exists():
        missing.append(f"{SIGNER_JAR} (rode ./setup.sh)")
    if need_adb and not shutil.which("adb"):
        missing.append("adb (brew install --cask android-platform-tools)")
    if missing:
        log("Ferramentas faltando:\n  - " + "\n  - ".join(missing))
        sys.exit(1)


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
    app = root.find("application")

    uses_sdk = root.find("uses-sdk")
    if uses_sdk is not None and uses_sdk.get(A + "targetSdkVersion"):
        uses_sdk.set(A + "targetSdkVersion", str(target))

    if app is None:
        return

    def set_attr(el, name, value, why):
        if el.get(A + name) != value:
            el.set(A + name, value)
            changes.append(why)

    set_attr(app, "extractNativeLibs", "true",
             "extractNativeLibs=true (bibliotecas nativas extraídas na instalação)")

    if app.get(A + "testOnly") is not None:
        del app.attrib[A + "testOnly"]
        changes.append("removido testOnly (bloqueava instalação normal)")

    if target >= 28:
        if app.get(A + "usesCleartextTraffic") is None:
            set_attr(app, "usesCleartextTraffic", "true",
                     "usesCleartextTraffic=true (permite HTTP sem TLS, comum em apps antigos)")
        libs = {el.get(A + "name") for el in app.findall("uses-library")}
        if "org.apache.http.legacy" not in libs:
            lib = ET.SubElement(app, "uses-library")
            lib.set(A + "name", "org.apache.http.legacy")
            lib.set(A + "required", "false")
            changes.append("adicionada biblioteca org.apache.http.legacy")

    if target >= 29 and app.get(A + "requestLegacyExternalStorage") is None:
        set_attr(app, "requestLegacyExternalStorage", "true",
                 "requestLegacyExternalStorage=true (acesso antigo ao armazenamento)")

    if target >= 31:
        count = 0
        for tag in ("activity", "activity-alias", "service", "receiver"):
            for el in app.findall(tag):
                if el.find("intent-filter") is not None and el.get(A + "exported") is None:
                    el.set(A + "exported", "true")
                    count += 1
        if count:
            changes.append(f"android:exported adicionado em {count} componente(s)")


def manifest_report(tree):
    root = tree.getroot()
    perms = {
        (el.get(A + "name") or "").rsplit(".", 1)[-1]
        for el in root.findall("uses-permission")
    }
    is_split = root.get("split") is not None or root.get(A + "isSplitRequired") == "true"
    app = root.find("application")
    if app is not None and app.find("meta-data[@{%s}name='com.android.vending.splits.required']" % ANDROID_NS) is not None:
        is_split = True
    return {
        "package": root.get("package"),
        "dangerous": sorted(perms & DANGEROUS_PERMISSIONS),
        "split": is_split,
    }


def native_abis(decoded_dir):
    lib = decoded_dir / "lib"
    if not lib.is_dir():
        return []
    return sorted(p.name for p in lib.iterdir() if p.is_dir() and any(p.glob("*.so")))


# ---------------------------------------------------------------- pipeline

def decode(apk, out_dir):
    res = run(["apktool", "d", "-f", "-s", "-o", str(out_dir), str(apk)])
    if res.returncode != 0:
        raise RuntimeError("falha ao descompilar:\n" + (res.stderr or res.stdout)[-2000:])


def build(decoded_dir, out_apk):
    res = run(["apktool", "b", "-o", str(out_apk), str(decoded_dir)])
    if res.returncode != 0:
        raise RuntimeError("falha ao recompilar:\n" + (res.stderr or res.stdout)[-2000:])


def sign(unsigned_apk, out_dir):
    res = run(["java", "-jar", str(SIGNER_JAR), "-a", str(unsigned_apk),
               "-o", str(out_dir), "--allowResign"])
    signed = list(out_dir.glob("*-aligned-*Signed.apk"))
    if res.returncode != 0 or not signed:
        raise RuntimeError("falha ao assinar:\n" + (res.stderr or res.stdout)[-2000:])
    return signed[0]


def print_analysis(info, min_sdk, cur_target, abis):
    log(f"  Pacote:        {info['package']}")
    log(f"  minSdk:        {min_sdk}")
    log(f"  targetSdk:     {cur_target}")
    log(f"  Arquiteturas:  {', '.join(abis) if abis else 'nenhuma lib nativa (Java/Kotlin puro)'}")


def warnings_for(info, cur_target, new_target, abis):
    warns = []
    if abis and not (set(abis) & ABI_64):
        warns.append(
            "Só tem bibliotecas nativas 32 bits (" + ", ".join(abis) + "). Celulares "
            "só 64 bits (ex.: Pixel 7 ou mais novo e vários aparelhos de 2024+) NÃO "
            "conseguem rodar este app, e isso não dá para corrigir reempacotando.")
    if info["split"]:
        warns.append(
            "Este APK parece ser parte de um pacote dividido (split APK / .xapk / "
            ".apks). Ele sozinho pode não funcionar.")
    if (cur_target or 0) < 23 <= new_target and info["dangerous"]:
        warns.append(
            "O app passa a usar permissões em tempo de execução e pode não pedir: "
            + ", ".join(info["dangerous"]) + ". Instale com --instalar (usa adb -g) "
            "ou conceda manualmente em Configurações > Apps > Permissões.")
    if new_target >= 31:
        warns.append(
            "targetSdk >= 31 costuma quebrar apps antigos (PendingIntent, etc.). "
            "Prefira 24 a 30, a menos que precise de mais.")
    return warns


def process(apk, args):
    log(f"\n=== {apk.name} ===")
    with tempfile.TemporaryDirectory(prefix="update-apk-") as tmp:
        tmp = Path(tmp)
        decoded = tmp / "src"
        decode(apk, decoded)

        yml_path = decoded / "apktool.yml"
        yml = yml_path.read_text()
        min_sdk, cur_target = read_sdk_info(yml)
        manifest_path = decoded / "AndroidManifest.xml"
        tree = load_manifest(manifest_path)
        info = manifest_report(tree)
        abis = native_abis(decoded)
        print_analysis(info, min_sdk, cur_target, abis)

        new_target = max(cur_target or min_sdk or 1, args.target)
        if args.force_target:
            new_target = args.target

        if args.analisar:
            for w in warnings_for(info, cur_target, new_target, abis):
                log(f"  ! {w}")
            if (cur_target or 0) < DEFAULT_TARGET:
                log(f"  -> Precisa de atualização (Android 14+ bloqueia targetSdk < 23, Android 15+ < 24).")
            return None

        changes = []
        if new_target != cur_target:
            yml = set_target_in_yml(yml, new_target)
            changes.append(f"targetSdk {cur_target} -> {new_target}")
        new_yml = compress_native_libs_in_yml(yml)
        if new_yml != yml and abis:
            changes.append("bibliotecas .so passam a ser comprimidas")
        yml_path.write_text(new_yml)

        patch_manifest(tree, new_target, changes)
        tree.write(manifest_path, encoding="utf-8", xml_declaration=True)

        log("  Alterações:")
        for c in changes or ["nenhuma (apenas reassinado)"]:
            log(f"    - {c}")

        unsigned = tmp / "unsigned.apk"
        build(decoded, unsigned)
        signed = sign(unsigned, tmp / "signed")

        args.out.mkdir(parents=True, exist_ok=True)
        final = args.out / f"{apk.stem}-atualizado.apk"
        shutil.move(str(signed), final)
        log(f"  OK -> {final.relative_to(ROOT) if final.is_relative_to(ROOT) else final}")

        for w in warnings_for(info, cur_target, new_target, abis):
            log(f"  ! {w}")

        if args.instalar:
            install(final, info["package"])
        return final


def install(apk, package):
    devices = [l for l in run(["adb", "devices"]).stdout.splitlines()[1:] if l.strip().endswith("device")]
    if not devices:
        log("  ! Nenhum celular conectado via adb (ative a Depuração USB).")
        return
    res = run(["adb", "install", "-r", "-g", str(apk)])
    out = (res.stdout + res.stderr).strip()
    if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:
        log(f"  Assinatura diferente da versão instalada; desinstalando {package} antes...")
        run(["adb", "uninstall", package])
        res = run(["adb", "install", "-r", "-g", str(apk)])
        out = (res.stdout + res.stderr).strip()
    log(f"  adb: {out.splitlines()[-1] if out else res.returncode}")


def collect_inputs(paths):
    if not paths:
        paths = [DEFAULT_INPUT]
    apks = []
    for p in map(Path, paths):
        if p.is_dir():
            apks += sorted(p.glob("*.apk"))
        elif p.suffix.lower() in (".xapk", ".apks", ".apkm"):
            log(f"! {p.name}: pacotes {p.suffix} ainda não são suportados; extraia o APK base.")
        elif p.exists():
            apks.append(p)
        else:
            log(f"! {p} não encontrado")
    return apks


def main():
    parser = argparse.ArgumentParser(
        description="Atualiza APKs antigos para Android moderno.")
    parser.add_argument("entradas", nargs="*",
                        help="arquivos .apk ou pastas (padrão: ./apks)")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET,
                        help=f"targetSdk mínimo desejado (padrão: {DEFAULT_TARGET})")
    parser.add_argument("--force-target", action="store_true",
                        help="usa exatamente --target, mesmo se o app já tiver um maior")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT,
                        help="pasta de saída (padrão: ./atualizados)")
    parser.add_argument("--analisar", action="store_true",
                        help="só mostra informações, sem gerar APK")
    parser.add_argument("--instalar", action="store_true",
                        help="instala no celular via adb concedendo permissões")
    args = parser.parse_args()

    check_tools(need_adb=args.instalar)
    apks = collect_inputs(args.entradas)
    if not apks:
        DEFAULT_INPUT.mkdir(exist_ok=True)
        log(f"Nenhum APK encontrado. Coloque arquivos .apk em {DEFAULT_INPUT}")
        return

    ok, failed = 0, []
    for apk in apks:
        try:
            process(apk, args)
            ok += 1
        except Exception as e:
            log(f"  ERRO: {e}")
            failed.append(apk.name)

    log(f"\nConcluído: {ok} ok, {len(failed)} com erro.")
    if failed:
        log("Falharam: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
