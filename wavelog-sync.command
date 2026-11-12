#!/usr/bin/env bash
# Corre wavelog_sync.py dentro de su entorno virtual, creándolo si hace falta.
# Uso: ./wavelog-sync.sh [opciones de wavelog_sync.py]   (ej: ./wavelog-sync.sh --dry-run)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
PY="$VENV/bin/python"
SELLO="$VENV/.requirements.sha"

cd "$DIR"

if [[ ! -x "$PY" ]]; then
    echo "Creando entorno virtual en .venv..."
    python3 -m venv "$VENV"
fi

# Reinstala dependencias sólo cuando cambia requirements.txt.
hash_req="$(shasum requirements.txt | cut -d' ' -f1)"
if [[ ! -f "$SELLO" || "$(cat "$SELLO")" != "$hash_req" ]]; then
    echo "Instalando dependencias..."
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r requirements.txt
    "$PY" -m playwright install chromium
    echo "$hash_req" > "$SELLO"
fi

if [[ ! -f .env ]]; then
    echo "Falta .env: copiá .env.example a .env y completalo." >&2
    exit 1
fi

exec "$PY" wavelog_sync.py "$@"
