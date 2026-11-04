#!/bin/bash
# Publica la web de progreso de la simulación en GitHub Pages (rama gh-pages) cada pocos minutos.
#
#   ./tools/publicar_web.sh            # bucle: exporta y publica cada INTERVALO segundos (300 por defecto)
#   ./tools/publicar_web.sh --una-vez  # una sola publicación
#
# Requisitos: el bot corriendo en papel (./start.sh) y permiso de push al repo. La primera vez crea la rama
# gh-pages; después hay que activar Pages en GitHub: Settings → Pages → Branch: gh-pages, carpeta /(root).
# Solo sube publico/ (la página y los dos JSON de la simulación): nunca el .env ni la base de datos.

set -euo pipefail
cd "$(dirname "$0")/.."
RAIZ="$(pwd)"
RAMA="gh-pages"
DESTINO="$RAIZ/.gh-pages"
INTERVALO="${INTERVALO:-300}"

if [ -f venv/bin/activate ]; then source venv/bin/activate; fi
set -a; [ -f .env ] && source .env; set +a

preparar() {
  if [ -d "$DESTINO/.git" ] || [ -f "$DESTINO/.git" ]; then return; fi
  git fetch origin "$RAMA" 2>/dev/null || true
  if git show-ref --verify --quiet "refs/remotes/origin/$RAMA"; then
    git worktree add "$DESTINO" "$RAMA" 2>/dev/null || git worktree add -B "$RAMA" "$DESTINO" "origin/$RAMA"
  else
    git worktree add --detach "$DESTINO"
    (cd "$DESTINO" && git checkout --orphan "$RAMA" && git rm -rqf . && git clean -fdq)
  fi
}

publicar() {
  python3 tools/exportar_progreso.py --salida "$RAIZ/publico" > /dev/null
  cp publico/index.html publico/progreso.json publico/historial.json "$DESTINO/"
  touch "$DESTINO/.nojekyll"
  cd "$DESTINO"
  git add -A
  if ! git diff --cached --quiet; then
    # Una sola revisión en gh-pages: se reescribe en cada publicación para que la rama no crezca sin fin.
    if git rev-parse --verify -q HEAD > /dev/null; then
      git commit -q --amend -m "Progreso $(date -u +%Y-%m-%dT%H:%MZ)"
    else
      git commit -qm "Progreso $(date -u +%Y-%m-%dT%H:%MZ)"
    fi
    git push -q --force origin "$RAMA" && echo "[web] publicado $(date '+%H:%M')" || echo "[web] fallo al subir; se reintenta"
  fi
  cd "$RAIZ"
}

preparar
if [ "${1:-}" = "--una-vez" ]; then publicar; exit 0; fi
while true; do
  publicar || echo "[web] error publicando; se reintenta en ${INTERVALO}s"
  sleep "$INTERVALO"
done
