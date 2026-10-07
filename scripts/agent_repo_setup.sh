#!/usr/bin/env bash
# === Prepara un bare repo para que los agentes de Jarvis puedan empujar ramas ===
#
# Los agentes corren en el contenedor jarvis-worker como un usuario sin
# privilegios (UID 1000 por defecto) y necesitan escribir en el bare repo para
# empujar su rama. Este script (D12 del Hito 9):
#   1. Da escritura al grupo del worker SOLO en objects/ y refs/ (directorios con
#      setgid para que lo que cree root después herede el grupo) y fija
#      core.sharedRepository=group para que git cree todo con permisos de grupo.
#   2. Instala un hook pre-receive que, si quien empuja es el UID del worker, solo
#      acepta refs/heads/agent/*. Se comprueba el UID (no una variable de
#      entorno): el agente no puede cambiarlo, porque no es root en su contenedor.
#   3. Deja hooks/ y config solo para root: el agente no puede desactivar el hook.
#
# Uso (como root en el servidor git):
#   scripts/agent_repo_setup.sh /ruta/a/repo.git [UID]
# Es idempotente. Después, monta el bare SIN :ro en el servicio jarvis-worker y
# añade el repo a JARVIS_AGENT_REPOS.

set -euo pipefail

REPO="${1:?Uso: $0 /ruta/a/repo.git [UID]}"
AGENT_UID="${2:-1000}"

[ "$(id -u)" = 0 ] || { echo "Ejecútalo como root."; exit 1; }
[ -f "$REPO/HEAD" ] && [ -d "$REPO/objects" ] || { echo "$REPO no parece un bare repo."; exit 1; }

# 1. Escritura del grupo solo donde git escribe al recibir un push.
git --git-dir="$REPO" config core.sharedRepository group
for dir in objects refs; do
  chgrp -R "$AGENT_UID" "$REPO/$dir"
  find "$REPO/$dir" -type d -exec chmod g+rwxs {} +
  find "$REPO/$dir" -type f -exec chmod g+rw {} +
done

# 2. Hook: el worker solo puede tocar ramas agent/*.
cat > "$REPO/hooks/pre-receive" <<HOOK
#!/bin/sh
# Instalado por jarvis/scripts/agent_repo_setup.sh. Los agentes de Jarvis
# (UID $AGENT_UID) solo pueden crear o actualizar ramas agent/*.
[ "\$(id -u)" = "$AGENT_UID" ] || exit 0
while read -r old new ref; do
  case "\$ref" in
    refs/heads/agent/*) ;;
    *) echo "Los agentes solo pueden empujar ramas agent/* (rechazado: \$ref)" >&2; exit 1 ;;
  esac
done
exit 0
HOOK

# 3. hooks/ y config, solo de root.
chown root:root "$REPO/hooks" "$REPO/hooks/pre-receive" "$REPO/config"
chmod 755 "$REPO/hooks" "$REPO/hooks/pre-receive"
chmod 644 "$REPO/config"

echo "✅ $REPO listo para agentes (UID $AGENT_UID): escritura en objects/ y refs/, hook pre-receive instalado."
