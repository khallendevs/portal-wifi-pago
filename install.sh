#!/data/data/com.termux/files/usr/bin/bash
# Instalador do Portal Wi-Fi Pago para Termux.
set -Eeuo pipefail

REPO_RAW="https://raw.githubusercontent.com/khallendevs/portal-wifi-pago/main"
PORTAL_URL="$REPO_RAW/portal.py"
INSTALL_DIR="${PORTAL_INSTALL_DIR:-$HOME/portal-wifi-pago}"

if ! command -v pkg >/dev/null 2>&1 || [[ -z "${PREFIX:-}" ]]; then
  echo "Erro: este instalador deve ser executado no Termux para Android." >&2
  exit 1
fi
printf '%s\n' "Instalando dependências do portal no Termux..."
pkg update -y
pkg install -y root-repo
pkg install -y python iptables tsu curl
if ! command -v curl >/dev/null 2>&1; then
  echo "Erro: não foi possível instalar curl pelo gerenciador do Termux." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" "$PREFIX/bin"
chmod 700 "$INSTALL_DIR"
PORTAL_FILE="$INSTALL_DIR/portal.py"
TEMP_FILE="$PORTAL_FILE.download.$$"
trap 'rm -f "$TEMP_FILE"' EXIT

printf '%s\n' "Baixando a versão atual de $PORTAL_URL"
curl --fail --silent --show-error --location --retry 3 --output "$TEMP_FILE" "$PORTAL_URL"
python - "$TEMP_FILE" <<'PY'
import ast, pathlib, re, sys
path = pathlib.Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
ast.parse(source, filename=str(path))
if not re.search(r'^ID_PROGRAMA\s*=\s*["\']wifi-portal-termux["\']\s*$', source, re.M):
    raise SystemExit("O arquivo baixado não corresponde ao Portal Wi-Fi Pago.")
match = re.search(r'^VERSION\s*=\s*["\']([0-9]+(?:\.[0-9]+)+)["\']\s*$', source, re.M)
if not match:
    raise SystemExit("O arquivo baixado não declara uma versão válida.")
print("Versão validada:", match.group(1))
PY

if [[ -f "$PORTAL_FILE" ]]; then
  BACKUP="$PORTAL_FILE.bak-install-$(date +%Y%m%d-%H%M%S)"
  cp -p "$PORTAL_FILE" "$BACKUP"
  echo "Cópia anterior guardada em: $BACKUP"
fi
chmod 600 "$TEMP_FILE"
mv -f "$TEMP_FILE" "$PORTAL_FILE"
trap - EXIT

LAUNCHER="$PREFIX/bin/wifi-portal"
LAUNCHER_TMP="$LAUNCHER.new.$$"
cat > "$LAUNCHER_TMP" <<'LAUNCHER_EOF'
#!/data/data/com.termux/files/usr/bin/bash
set -e
PORTAL_DIR="${PORTAL_INSTALL_DIR:-$HOME/portal-wifi-pago}"
export WIFI_IFACE="${WIFI_IFACE:-wlan0}"
exec python3 "$PORTAL_DIR/portal.py" "$@"
LAUNCHER_EOF
chmod 700 "$LAUNCHER_TMP"
mv -f "$LAUNCHER_TMP" "$LAUNCHER"

cat <<EOF

Instalação concluída.
  Script:    $PORTAL_FILE
  Comando:   wifi-portal
  Interface: WIFI_IFACE=wlan0 wifi-portal servidor

Troque a senha padrão do painel (admin123) antes de liberar o hotspot.
O banco de dados fica em ~/wifi_portal.db e não foi alterado pelo instalador.
EOF
