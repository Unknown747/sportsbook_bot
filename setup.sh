#!/usr/bin/env bash
# ============================================================
#  setup.sh — Sportsbook Bot VPS Setup & Launcher
#  Jalankan: bash setup.sh
# ============================================================
set -e

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✔ $*${RESET}"; }
warn() { echo -e "${YELLOW}⚠ $*${RESET}"; }
fail() { echo -e "${RED}✘ $*${RESET}"; exit 1; }
info() { echo -e "${BOLD}→ $*${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo -e "${BOLD}========================================"
echo -e " Sportsbook Bot — VPS Setup"
echo -e "========================================${RESET}"
echo ""

# ── 1. Cari Python 3 ─────────────────────────────────────────
info "Memeriksa Python..."
PYTHON=""
for cmd in python3 python python3.12 python3.11 python3.10 python3.9; do
    if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c "import sys; assert sys.version_info >= (3,7)" 2>/dev/null; then
            PYTHON="$cmd"
            VER=$("$cmd" -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}')")
            ok "Python ditemukan: $cmd (v$VER)"
            break
        fi
    fi
done
[ -z "$PYTHON" ] && fail "Python 3.7+ tidak ditemukan. Install dulu: sudo apt install python3"

# ── 2. Cek file .env ─────────────────────────────────────────
info "Memeriksa file .env..."
if [ ! -f ".env" ]; then
    fail ".env tidak ditemukan di $(pwd). Buat dulu:\n  cp .env.example .env && nano .env"
fi
ok ".env ditemukan"

# Cek key wajib tidak kosong (tanpa menampilkan nilainya)
check_env_key() {
    local KEY="$1"
    local VAL
    VAL=$(grep -E "^${KEY}=" .env 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]')
    if [ -z "$VAL" ] || [[ "$VAL" == *"isi_dengan"* ]]; then
        fail "${KEY} belum diisi di .env. Buka nano .env dan isi nilainya."
    fi
    ok "${KEY} terisi (${#VAL} karakter)"
}

check_env_key "ODDS_API_KEY"
check_env_key "TELEGRAM_BOT_TOKEN"
check_env_key "TELEGRAM_CHAT_ID"

# ── 3. Install dependencies ──────────────────────────────────
info "Menginstall dependencies..."

# Coba pip3, fallback ke pip
PIP=""
for cmd in pip3 pip; do
    if command -v "$cmd" &>/dev/null; then
        PIP="$cmd"
        break
    fi
done

if [ -z "$PIP" ]; then
    warn "pip tidak ditemukan, mencoba install..."
    sudo apt-get install -y python3-pip 2>/dev/null || \
        $PYTHON -m ensurepip --upgrade 2>/dev/null || \
        fail "Tidak bisa install pip. Coba: sudo apt install python3-pip"
    PIP="$PYTHON -m pip"
fi

# Install ke user site (tidak perlu sudo, tidak konflik dengan sistem)
$PYTHON -m pip install --quiet --user -r requirements.txt
ok "Dependencies terinstall"

# ── 4. Verifikasi python-dotenv bisa load .env ───────────────
info "Memverifikasi .env terbaca oleh Python..."
VERIFY=$($PYTHON - "$SCRIPT_DIR/.env" <<'EOF'
import os, sys

dotenv_path = sys.argv[1]

try:
    from dotenv import load_dotenv
except ImportError:
    print("DOTENV_MISSING")
    sys.exit(1)

load_dotenv(dotenv_path=dotenv_path, override=True)

missing = []
for k in ["ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
    if not os.environ.get(k, "").strip():
        missing.append(k)

if missing:
    print("MISSING:" + ",".join(missing))
    sys.exit(2)

print("OK")
EOF
)

case "$VERIFY" in
    "OK")
        ok "python-dotenv berhasil membaca semua key dari .env" ;;
    "DOTENV_MISSING")
        warn "python-dotenv belum terinstall ke Python ini — coba install ulang..."
        $PYTHON -m pip install --user python-dotenv
        ok "python-dotenv berhasil diinstall" ;;
    "MISSING:"*)
        KEYS="${VERIFY#MISSING:}"
        fail "Key berikut kosong di .env setelah dibaca Python: ${KEYS}\nCek tidak ada spasi di sekitar tanda = dan tidak ada quote ekstra." ;;
    *)
        fail "Verifikasi gagal: $VERIFY" ;;
esac

# ── 5. Ringkasan & jalankan ──────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}========================================"
echo -e " Setup selesai — semua OK"
echo -e "========================================${RESET}"
echo ""
echo "Pilihan jalankan bot:"
echo ""
echo -e "  ${BOLD}A) Langsung (test, bisa di-Ctrl+C):${RESET}"
echo "     $PYTHON main.py"
echo ""
echo -e "  ${BOLD}B) Background 24/7 via systemd (disarankan):${RESET}"
echo "     sudo cp sportsbook-bot.service /etc/systemd/system/"
echo "     sudo sed -i 's|/home/botuser/sportsbook-bot|$(pwd)|g' /etc/systemd/system/sportsbook-bot.service"
echo "     sudo sed -i 's|botuser|'\"$USER\"'|g' /etc/systemd/system/sportsbook-bot.service"
echo "     sudo sed -i 's|ExecStart=.*|ExecStart=$PYTHON $(pwd)/main.py|g' /etc/systemd/system/sportsbook-bot.service"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now sportsbook-bot"
echo "     sudo journalctl -u sportsbook-bot -f"
echo ""

# Tanya mau langsung jalankan atau tidak
read -rp "Jalankan bot sekarang? [y/N] " RUN
if [[ "$RUN" =~ ^[Yy]$ ]]; then
    echo ""
    info "Menjalankan bot... (Ctrl+C untuk berhenti)"
    echo ""
    exec $PYTHON main.py
fi
