#!/bin/bash
cd "$(dirname "$0")"
python3 update_apk.py "$@"
[ -d atualizados ] && open atualizados
echo
read -n 1 -s -r -p "Pressione qualquer tecla para fechar..."
