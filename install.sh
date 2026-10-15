#!/usr/bin/env bash
# Instala (o actualiza) Hound en un servidor Linux como servicio de systemd.
#
#  - Descarga uv y, con él, un Python 3.12 propio (no toca el Python del sistema).
#  - Instala Hound y sus dependencias en un entorno aislado (/opt/hound).
#  - Instala Tesseract (OCR) con los idiomas indicados, con el gestor de paquetes del sistema.
#  - Descarga el modelo de embeddings una sola vez.
#  - Registra el servicio "hound" (arranque automático y reinicio si falla) y abre el puerto
#    en el cortafuegos (ufw o firewalld) solo para la red de la VPN.
#
# Volver a ejecutarlo actualiza Hound y conserva la configuración, el índice y los tokens.
#
# Uso rápido (como root o con sudo):
#   curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/install.sh | sudo bash
#
# Con parámetros:
#   curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/install.sh | \
#       sudo bash -s -- --share /srv/compartido --allow-from 10.8.0.0/24
#
# Los parámetros también se pueden pasar con variables de entorno HOUND_SHARE_PATH,
# HOUND_ALLOW_FROM, HOUND_PORT, HOUND_SHARE_NAME, HOUND_UNC, HOUND_VERSION.
set -euo pipefail

SHARE_PATH="${HOUND_SHARE_PATH:-}"
SHARE_NAME="${HOUND_SHARE_NAME:-}"
UNC="${HOUND_UNC:-}"
PORT="${HOUND_PORT:-8765}"
ALLOW_FROM="${HOUND_ALLOW_FROM:-}"
VERSION="${HOUND_VERSION:-main}"
SOURCE=""
REPO="abosch19/hound"
INSTALL_DIR="/opt/hound"
DATA_DIR="/var/lib/hound"
CONFIG_DIR="/etc/hound"
OCR_LANGUAGES="spa,eng"
NO_OCR=0
NO_SERVICE=0
NO_FIREWALL=0

UV_VERSION="0.9.26"
PYTHON_VERSION="3.12"

usage() {
    cat <<'EOF'
Uso: install.sh [opciones]

  --share RUTA          Carpeta compartida a indexar (si se omite, se detecta en Samba)
  --share-name NOMBRE   Nombre del recurso compartido de Samba
  --unc RUTA            Ruta UNC que ven los usuarios (por defecto \\SERVIDOR\RECURSO)
  --port N              Puerto del servidor MCP (8765)
  --allow-from REDES    Red(es) que pueden conectar, p. ej. 10.8.0.0/24 (separadas por comas)
  --version REF         Rama o etiqueta de GitHub a instalar (main)
  --source CARPETA      Instalar desde una carpeta local con el código
  --ocr-languages L     Idiomas de OCR, códigos de Tesseract separados por comas (spa,eng)
  --no-ocr              No instalar Tesseract
  --no-service          No registrar el servicio de systemd
  --no-firewall         No tocar el cortafuegos
  --install-dir DIR     Programa (/opt/hound)
  --data-dir DIR        Índice, modelo, tokens y registros (/var/lib/hound)
  --config-dir DIR      Configuración (/etc/hound)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --share) SHARE_PATH="$2"; shift 2 ;;
        --share-name) SHARE_NAME="$2"; shift 2 ;;
        --unc) UNC="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --allow-from) ALLOW_FROM="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        --config-dir) CONFIG_DIR="$2"; shift 2 ;;
        --ocr-languages) OCR_LANGUAGES="$2"; shift 2 ;;
        --no-ocr) NO_OCR=1; shift ;;
        --no-service) NO_SERVICE=1; shift ;;
        --no-firewall) NO_FIREWALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Opción desconocida: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then C_STEP=$'\e[36m'; C_WARN=$'\e[33m'; C_OK=$'\e[32m'; C_END=$'\e[0m'; else C_STEP=; C_WARN=; C_OK=; C_END=; fi
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$1" "$C_END"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '    %sAVISO: %s%s\n' "$C_WARN" "$1" "$C_END"; }
die() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

download() {
    info "Descargando $1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        die "Hace falta curl o wget"
    fi
}

# Con "curl ... | bash" la entrada estándar es el propio script: las preguntas se leen de la terminal.
ask() {
    local prompt="$1" answer=""
    if [ -r /dev/tty ]; then
        read -r -p "    $prompt" answer </dev/tty || true
    fi
    printf '%s' "$answer"
}

CONFIG="$CONFIG_DIR/config.toml"
VENV="$INSTALL_DIR/venv"
PY="$VENV/bin/python"
STATE="$CONFIG_DIR/install.state"

# --- comprobaciones ---------------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "Ejecuta este script como root (con sudo)."
[ "$(uname -s)" = "Linux" ] || die "Este instalador es para Linux. En Windows usa install.ps1."
case "$(uname -m)" in
    x86_64|amd64) ARCH="x86_64" ;;
    aarch64|arm64) ARCH="aarch64" ;;
    *) die "Arquitectura no soportada: $(uname -m) (Hound necesita x86_64 o aarch64)." ;;
esac
if [ "$NO_SERVICE" -eq 0 ] && ! command -v systemctl >/dev/null 2>&1; then
    die "No se encuentra systemd. Usa --no-service y arranca 'hound run' con tu gestor de servicios."
fi

step "Carpeta compartida"
if [ -z "$SHARE_PATH" ]; then
    # Recursos de Samba (testparm), sin los especiales.
    SHARES=()
    if command -v testparm >/dev/null 2>&1; then
        while IFS='|' read -r name path; do
            if [ -n "$path" ] && [ -d "$path" ]; then SHARES+=("$name|$path"); fi
        done < <(testparm -s 2>/dev/null | awk '
            /^\[/ { name = substr($0, 2, length($0) - 2); next }
            /^[ \t]*path[ \t]*=/ { sub(/^[ \t]*path[ \t]*=[ \t]*/, "");
                if (name != "global" && name != "homes" && name != "printers" && name != "print$") print name "|" $0 }')
    fi
    if [ "${#SHARES[@]}" -eq 1 ]; then
        SHARE_PATH="${SHARES[0]#*|}"
        [ -n "$SHARE_NAME" ] || SHARE_NAME="${SHARES[0]%%|*}"
        info "Detectada: $SHARE_NAME -> $SHARE_PATH"
    elif [ "${#SHARES[@]}" -gt 1 ]; then
        info "Recursos compartidos de Samba en este servidor:"
        for i in "${!SHARES[@]}"; do info "  [$((i + 1))] ${SHARES[$i]%%|*} -> ${SHARES[$i]#*|}"; done
        choice="$(ask "¿Cuál quieres indexar? (número) ")"
        [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#SHARES[@]}" ] ||
            die "Opción no válida. Indica la carpeta con --share RUTA."
        SHARE_PATH="${SHARES[$((choice - 1))]#*|}"
        [ -n "$SHARE_NAME" ] || SHARE_NAME="${SHARES[$((choice - 1))]%%|*}"
    else
        SHARE_PATH="$(ask "Ruta de la carpeta compartida (p. ej. /srv/compartido): ")"
    fi
fi
[ -n "$SHARE_PATH" ] || die "Indica la carpeta compartida con --share RUTA."
[ -d "$SHARE_PATH" ] || die "No existe la carpeta $SHARE_PATH"
SHARE_PATH="$(cd "$SHARE_PATH" && pwd -P)"
if [ -z "$SHARE_NAME" ] && command -v testparm >/dev/null 2>&1; then
    SHARE_NAME="$(testparm -s 2>/dev/null | awk -v want="$SHARE_PATH" '
        /^\[/ { name = substr($0, 2, length($0) - 2); next }
        /^[ \t]*path[ \t]*=/ { sub(/^[ \t]*path[ \t]*=[ \t]*/, ""); sub(/\/+$/, ""); if ($0 == want) { print name; exit } }')"
fi
if [ -z "$UNC" ] && [ -n "$SHARE_NAME" ]; then UNC="\\\\$(hostname -s)\\$SHARE_NAME"; fi
info "Carpeta: $SHARE_PATH"
info "Ruta UNC: ${UNC:-(no detectada)}"

mkdir -p "$INSTALL_DIR/bin" "$DATA_DIR/logs" "$CONFIG_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Si el servicio ya existe, se para para poder actualizar los ficheros.
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet hound 2>/dev/null; then
    step "Parando el servicio hound para actualizar"
    systemctl stop hound
fi

# --- Python y Hound -----------------------------------------------------------------------

step "Instalando uv $UV_VERSION y Python $PYTHON_VERSION"
UV="$INSTALL_DIR/bin/uv"
if [ ! -x "$UV" ] || ! "$UV" --version 2>/dev/null | grep -q "$UV_VERSION"; then
    LIBC="gnu"
    if ldd --version 2>&1 | grep -qi musl; then LIBC="musl"; fi
    TRIPLE="$ARCH-unknown-linux-$LIBC"
    download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$TRIPLE.tar.gz" "$TMP/uv.tar.gz"
    tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
    install -m 755 "$TMP/uv-$TRIPLE/uv" "$TMP/uv-$TRIPLE/uvx" "$INSTALL_DIR/bin/"
fi
export UV_PYTHON_INSTALL_DIR="$INSTALL_DIR/python"
export UV_CACHE_DIR="$INSTALL_DIR/cache"
export UV_NO_PROGRESS=1
"$UV" python install "$PYTHON_VERSION" || die "No se pudo instalar Python"
if [ ! -x "$PY" ]; then
    "$UV" venv "$VENV" --python "$PYTHON_VERSION" || die "No se pudo crear el entorno de Python"
fi

if [ -n "$SOURCE" ]; then
    PKG="$(cd "$SOURCE" && pwd -P)"
    step "Instalando Hound ($PKG)"
else
    PKG="https://github.com/$REPO/archive/$VERSION.tar.gz"
    step "Instalando Hound ($REPO@$VERSION)"
fi
"$UV" pip install --python "$PY" --reinstall-package hound "$PKG" || die "No se pudo instalar Hound"

# Orden "hound" en el PATH, siempre con la configuración del servicio.
cat >/usr/local/bin/hound <<EOF
#!/bin/sh
# Generado por el instalador de Hound.
export HOUND_CONFIG="\${HOUND_CONFIG:-$CONFIG}"
export HF_HOME="\${HF_HOME:-$DATA_DIR/hf}"
exec "$PY" -m hound "\$@"
EOF
chmod 755 /usr/local/bin/hound
export HOUND_CONFIG="$CONFIG"
export HF_HOME="$DATA_DIR/hf"
hound() { "$PY" -m hound "$@"; }

# --- OCR ----------------------------------------------------------------------------------

if [ "$NO_OCR" -eq 0 ]; then
    step "Tesseract (OCR)"
    IFS=',' read -r -a LANGS <<<"$OCR_LANGUAGES"
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq || warn "apt-get update ha fallado; se intenta instalar igualmente"
        apt-get install -y -qq tesseract-ocr >/dev/null || die "No se pudo instalar tesseract-ocr"
        for l in "${LANGS[@]}"; do apt-get install -y -qq "tesseract-ocr-$l" >/dev/null 2>&1 || true; done
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        PM="$(command -v dnf || command -v yum)"
        if ! "$PM" install -y -q tesseract >/dev/null 2>&1; then
            # RHEL, Rocky, Alma: Tesseract está en EPEL.
            "$PM" install -y -q epel-release >/dev/null 2>&1 || true
            "$PM" install -y -q tesseract >/dev/null || die "No se pudo instalar tesseract (¿falta EPEL?)"
        fi
        for l in "${LANGS[@]}"; do "$PM" install -y -q "tesseract-langpack-$l" >/dev/null 2>&1 || true; done
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install tesseract-ocr >/dev/null || die "No se pudo instalar tesseract-ocr"
    elif command -v pacman >/dev/null 2>&1; then
        pacman -S --noconfirm --needed tesseract >/dev/null || die "No se pudo instalar tesseract"
        for l in "${LANGS[@]}"; do pacman -S --noconfirm --needed "tesseract-data-$l" >/dev/null 2>&1 || true; done
    elif ! command -v tesseract >/dev/null 2>&1; then
        die "No se reconoce el gestor de paquetes: instala Tesseract a mano o usa --no-ocr"
    fi
    command -v tesseract >/dev/null 2>&1 || die "Tesseract no ha quedado instalado"
    info "$(tesseract --version 2>&1 | head -n 1)"
    # Los idiomas que el sistema no trae en paquetes se descargan a su carpeta tessdata.
    TESSDATA="$(tesseract --list-langs 2>&1 | sed -n 's/.*"\(.*\)".*/\1/p' | head -n 1)"
    TESSDATA="${TESSDATA%/}"
    for l in "${LANGS[@]}"; do
        if ! tesseract --list-langs 2>&1 | grep -qx "$l"; then
            [ -n "$TESSDATA" ] && [ -d "$TESSDATA" ] || die "No se encuentra la carpeta tessdata de Tesseract"
            download "https://github.com/tesseract-ocr/tessdata/raw/main/$l.traineddata" "$TESSDATA/$l.traineddata"
        fi
    done
fi

# --- configuración y modelo --------------------------------------------------------------

step "Configuración"
if [ ! -f "$CONFIG" ]; then
    INIT=(--config "$CONFIG" init --share "$SHARE_PATH" --port "$PORT" --data-dir "$DATA_DIR" --permissions posix)
    [ -n "$UNC" ] && INIT+=(--unc "$UNC")
    [ -n "$SHARE_NAME" ] && INIT+=(--share-name "$SHARE_NAME")
    hound "${INIT[@]}" || die "No se pudo crear la configuración"
    if [ "$NO_OCR" -eq 0 ]; then
        sed -i "s/^languages = \"spa+eng\"/languages = \"${OCR_LANGUAGES//,/+}\"/" "$CONFIG"
    fi
else
    info "Se conserva la configuración existente: $CONFIG"
fi

step "Modelo de embeddings (se descarga una sola vez)"
hound --config "$CONFIG" model download || die "No se pudo descargar el modelo"

# Solo root puede leer el índice (fragmentos de documentos y hashes de tokens).
chown -R root:root "$DATA_DIR" "$CONFIG_DIR"
chmod 700 "$DATA_DIR"
chmod 755 "$CONFIG_DIR"
chmod 644 "$CONFIG"

# Una carpeta compartida grande necesita muchos "watches" de inotify para vigilar los cambios.
WATCHES="$(cat /proc/sys/fs/inotify/max_user_watches 2>/dev/null || echo 0)"
if [ "$WATCHES" -lt 524288 ]; then
    step "Ampliando fs.inotify.max_user_watches a 524288"
    echo "fs.inotify.max_user_watches = 524288" >/etc/sysctl.d/60-hound.conf
    sysctl -q -p /etc/sysctl.d/60-hound.conf || warn "No se pudo aplicar ahora; se aplicará al reiniciar"
fi

# --- servicio -----------------------------------------------------------------------------

if [ "$NO_SERVICE" -eq 0 ]; then
    step "Servicio de systemd"
    cat >/etc/systemd/system/hound.service <<EOF
[Unit]
Description=Hound: buscador semántico de la carpeta compartida (servidor MCP e indexador)
After=network-online.target remote-fs.target nss-user-lookup.target winbind.service sssd.service
Wants=network-online.target

[Service]
Type=simple
Environment=HOUND_CONFIG=$CONFIG
Environment=HF_HOME=$DATA_DIR/hf
ExecStart=$PY -m hound --config $CONFIG run
WorkingDirectory=$DATA_DIR
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
KillMode=mixed

# Corre como root para poder leer toda la carpeta y consultar los grupos de cualquier usuario,
# pero sin más privilegios que leer ficheros y con el resto del sistema en solo lectura.
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now hound >/dev/null 2>&1 || systemctl enable --now hound
    info "Servicio hound en marcha (arranque automático, se reinicia si falla)."

    if [ "$NO_FIREWALL" -eq 0 ]; then
        step "Cortafuegos"
        if [ -z "$ALLOW_FROM" ]; then
            # Por defecto, solo las subredes locales del servidor.
            NETS=()
            while read -r cidr; do
                NETS+=("$("$PY" -c 'import ipaddress, sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$cidr")")
            done < <(ip -o -f inet addr show scope global 2>/dev/null | awk '{print $4}')
            ALLOW_FROM="$(IFS=','; echo "${NETS[*]}")"
            DEFAULT_ALLOW=1
        fi
        NETS=()
        [ -n "$ALLOW_FROM" ] && IFS=',' read -r -a NETS <<<"${ALLOW_FROM// /}"
        # Se quitan primero las reglas de una instalación anterior.
        if [ -f "$STATE" ]; then
            # shellcheck disable=SC1090
            . "$STATE"
            OLD=()
            [ -n "${FW_NETS:-}" ] && IFS=',' read -r -a OLD <<<"$FW_NETS"
            for n in ${OLD[@]+"${OLD[@]}"}; do
                case "${FW_TOOL:-}" in
                    ufw) ufw delete allow proto tcp from "$n" to any port "$FW_PORT" >/dev/null 2>&1 || true ;;
                    firewalld) firewall-cmd -q --permanent --remove-rich-rule="rule family=ipv4 source address=$n port port=$FW_PORT protocol=tcp accept" 2>/dev/null || true ;;
                esac
            done
            [ "${FW_TOOL:-}" = "firewalld" ] && firewall-cmd -q --reload 2>/dev/null || true
        fi
        FW_TOOL=""
        if [ "${#NETS[@]}" -eq 0 ]; then
            warn "No se ha podido detectar la red local: indica --allow-from <red de la VPN>. No se abre el puerto."
        elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
            FW_TOOL="ufw"
            for n in "${NETS[@]}"; do ufw allow proto tcp from "$n" to any port "$PORT" comment "Hound MCP" >/dev/null; done
        elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
            FW_TOOL="firewalld"
            for n in "${NETS[@]}"; do
                firewall-cmd -q --permanent --add-rich-rule="rule family=ipv4 source address=$n port port=$PORT protocol=tcp accept"
            done
            firewall-cmd -q --reload
        fi
        if [ "${#NETS[@]}" -eq 0 ]; then
            :
        elif [ -n "$FW_TOOL" ]; then
            printf 'FW_TOOL=%s\nFW_PORT=%s\nFW_NETS=%s\n' "$FW_TOOL" "$PORT" "$(IFS=','; echo "${NETS[*]}")" >"$STATE"
            info "Puerto $PORT abierto ($FW_TOOL) para: ${NETS[*]}"
            if [ "${DEFAULT_ALLOW:-0}" -eq 1 ]; then
                warn "No has indicado --allow-from: solo se permite la subred local. Para la VPN, vuelve a ejecutar con --allow-from <red de la VPN>, p. ej. 10.8.0.0/24."
            fi
        else
            rm -f "$STATE"
            warn "No hay ufw ni firewalld activos: el puerto $PORT queda accesible desde cualquier red que llegue al servidor. Restríngelo a la VPN en tu cortafuegos."
        fi
    fi

    # El MCP es lo primero que arranca: en cuanto responde, la instalación está lista.
    step "Esperando al servidor MCP"
    for _ in $(seq 1 60); do
        if "$PY" -c 'import sys, urllib.request; urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=2)' "$PORT" 2>/dev/null; then
            info "Responde en http://$(hostname -f 2>/dev/null || hostname):$PORT/mcp"
            break
        fi
        sleep 2
    done
fi

step "Comprobación"
hound --config "$CONFIG" doctor || true

printf '\n%sHound instalado.%s\n' "$C_OK" "$C_END"
echo "  Configuración: $CONFIG"
echo "  Datos:         $DATA_DIR"
echo "  Registros:     $DATA_DIR/logs  (y journalctl -u hound)"
echo "  El índice se está construyendo en segundo plano. Progreso: sudo hound status"
echo "  (o la herramienta index_status desde Claude)"
echo ""
echo "Siguiente paso: crea un token para cada usuario:"
echo "  sudo hound token create USUARIO --host $(hostname)"
