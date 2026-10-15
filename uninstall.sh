#!/usr/bin/env bash
# Desinstala Hound de un servidor Linux.
#
# Para y elimina el servicio, las reglas del cortafuegos, el programa y la orden "hound".
# Por defecto CONSERVA los datos (configuración, índice, tokens, registros) en /var/lib/hound
# y /etc/hound; con --purge también los borra. Nunca toca la carpeta compartida.
# Tesseract no se desinstala (puede usarlo otro programa): se quita con el gestor de paquetes.
#
#   curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/uninstall.sh | sudo bash
#   curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/uninstall.sh | sudo bash -s -- --purge
set -euo pipefail

INSTALL_DIR="/opt/hound"
DATA_DIR="/var/lib/hound"
CONFIG_DIR="/etc/hound"
PURGE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        --config-dir) CONFIG_DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0" 2>/dev/null; echo "Uso: uninstall.sh [--purge]"; exit 0 ;;
        *) echo "Opción desconocida: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Ejecuta este script como root (con sudo)." >&2; exit 1; }

if [ -f /etc/systemd/system/hound.service ]; then
    echo "Parando y eliminando el servicio..."
    systemctl disable --now hound >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/hound.service
    systemctl daemon-reload
    systemctl reset-failed hound >/dev/null 2>&1 || true
fi

STATE="$CONFIG_DIR/install.state"
if [ -f "$STATE" ]; then
    echo "Quitando las reglas del cortafuegos..."
    # shellcheck disable=SC1090
    . "$STATE"
    NETS=()
    [ -n "${FW_NETS:-}" ] && IFS=',' read -r -a NETS <<<"$FW_NETS"
    for n in ${NETS[@]+"${NETS[@]}"}; do
        case "${FW_TOOL:-}" in
            ufw) ufw delete allow proto tcp from "$n" to any port "$FW_PORT" >/dev/null 2>&1 || true ;;
            firewalld) firewall-cmd -q --permanent --remove-rich-rule="rule family=ipv4 source address=$n port port=$FW_PORT protocol=tcp accept" 2>/dev/null || true ;;
        esac
    done
    [ "${FW_TOOL:-}" = "firewalld" ] && firewall-cmd -q --reload 2>/dev/null || true
    rm -f "$STATE"
fi

if [ -f /usr/local/bin/hound ] && grep -q "instalador de Hound" /usr/local/bin/hound; then
    rm -f /usr/local/bin/hound
fi
rm -f /etc/sysctl.d/60-hound.conf

if [ -d "$INSTALL_DIR" ]; then
    echo "Eliminando $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
fi

if [ "$PURGE" -eq 1 ]; then
    echo "Eliminando datos en $DATA_DIR y $CONFIG_DIR..."
    rm -rf "$DATA_DIR" "$CONFIG_DIR"
else
    echo "Se conservan la configuración, el índice y los tokens en $CONFIG_DIR y $DATA_DIR (usa --purge para borrarlos)."
fi
echo "Hound desinstalado."
