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

# ── 2. Cek / buat file .env ───────────────────────────────────
info "Memeriksa file .env..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        ok ".env dibuat dari .env.example"
    else
        touch .env
        ok ".env baru dibuat (kosong)"
    fi
else
    ok ".env ditemukan"
fi

# Ambil value sebuah key dari .env (tanpa menampilkan nilainya ke user)
get_env_value() {
    local KEY="$1"
    grep -E "^${KEY}=" .env 2>/dev/null | tail -n1 | cut -d'=' -f2- | tr -d '[:space:]'
}

# Set/replace sebuah key di .env
set_env_value() {
    local KEY="$1"
    local VAL="$2"
    if grep -qE "^${KEY}=" .env 2>/dev/null; then
        # Pakai delimiter '|' karena token API bisa mengandung karakter aneh, jarang '|'
        local ESCAPED
        ESCAPED=$(printf '%s' "$VAL" | sed 's/[&|\\]/\\&/g')
        sed -i "s|^${KEY}=.*|${KEY}=${ESCAPED}|" .env
    else
        echo "${KEY}=${VAL}" >> .env
    fi
}

# Pastikan key wajib terisi. Kalau kosong / placeholder, minta input langsung
# dari user (interaktif) lalu tulis ke .env — tidak perlu buka nano manual.
prompt_env_key() {
    local KEY="$1"
    local LABEL="$2"
    local REQUIRED="$3"   # "required" atau "optional"
    local VAL
    VAL=$(get_env_value "$KEY")

    if [ -n "$VAL" ] && [[ "$VAL" != *"isi_dengan"* ]]; then
        ok "${KEY} terisi (${#VAL} karakter)"
        return
    fi

    if [ ! -t 0 ]; then
        # Tidak ada terminal interaktif (mis. dijalankan lewat CI) — tidak bisa prompt.
        if [ "$REQUIRED" = "required" ]; then
            fail "${KEY} belum diisi di .env dan tidak ada input interaktif tersedia."
        else
            warn "${KEY} belum diisi — dilewati (opsional)."
        fi
        return
    fi

    echo ""
    info "${LABEL}"
    while true; do
        read -rp "  Masukkan ${KEY}: " VAL
        VAL=$(echo "$VAL" | tr -d '[:space:]')
        if [ -n "$VAL" ]; then
            break
        fi
        if [ "$REQUIRED" = "optional" ]; then
            warn "${KEY} dilewati (opsional, kosong)."
            return
        fi
        warn "${KEY} wajib diisi, tidak boleh kosong."
    done

    set_env_value "$KEY" "$VAL"
    ok "${KEY} disimpan ke .env (${#VAL} karakter)"
}

# Validasi ODDS_API_KEY langsung ke the-odds-api.com (pakai endpoint /v4/sports
# yang tidak makan quota request) — supaya user langsung tahu kalau key salah
# atau expired, tidak perlu jalankan bot dulu baru ketahuan.
validate_odds_api_key() {
    local KEY="$1"
    local HTTP_CODE
    HTTP_CODE=$(curl -s -o /tmp/odds_api_check.$$ -w "%{http_code}" \
        "https://api.the-odds-api.com/v4/sports/?apiKey=${KEY}" --max-time 10 2>/dev/null)
    local BODY
    BODY=$(cat /tmp/odds_api_check.$$ 2>/dev/null)
    rm -f /tmp/odds_api_check.$$

    case "$HTTP_CODE" in
        200)
            ok "ODDS_API_KEY valid — koneksi ke the-odds-api.com berhasil."
            return 0 ;;
        401)
            warn "ODDS_API_KEY ditolak (401 Unauthorized) — key salah atau belum aktif."
            return 1 ;;
        429)
            warn "ODDS_API_KEY kena rate limit / quota habis (429) — key mungkin masih valid tapi quota bulanan habis."
            return 0 ;;
        "")
            warn "Tidak bisa cek ODDS_API_KEY — tidak ada koneksi internet atau curl tidak tersedia. Dilewati."
            return 0 ;;
        *)
            warn "Cek ODDS_API_KEY dapat status HTTP ${HTTP_CODE} yang tidak terduga. Dilewati."
            return 0 ;;
    esac
}

# ── ODDS_API_KEY(S) — dukung 1 key atau rotasi banyak key ──────
# Kalau ODDS_API_KEYS (jamak) sudah terisi di .env, pakai itu langsung.
# Kalau belum, tanya user mau isi 1 key saja atau beberapa key untuk rotasi
# otomatis (supaya kalau satu key kena quota habis, bot pindah ke key
# berikutnya tanpa henti).
EXISTING_MULTI=$(get_env_value "ODDS_API_KEYS")

if [ -n "$EXISTING_MULTI" ]; then
    NUM_KEYS=$(echo "$EXISTING_MULTI" | tr ',' '\n' | grep -c '.')
    ok "ODDS_API_KEYS sudah terisi (${NUM_KEYS} key untuk rotasi)."
elif [ -t 0 ]; then
    echo ""
    info "API key data odds (wajib) — daftar gratis di https://the-odds-api.com"
    read -rp "  Mau pakai rotasi multi-key? (disarankan kalau punya lebih dari 1 akun/key) [y/N] " USE_MULTI

    ODDS_KEYS_ARR=()
    if [[ "$USE_MULTI" =~ ^[Yy]$ ]]; then
        echo "  Masukkan key satu per satu. Ketik kosong (Enter) kalau sudah selesai."
        while true; do
            read -rp "  Key #$((${#ODDS_KEYS_ARR[@]} + 1)) (kosongkan untuk selesai): " ONE_KEY
            ONE_KEY=$(echo "$ONE_KEY" | tr -d '[:space:]')
            if [ -z "$ONE_KEY" ]; then
                if [ ${#ODDS_KEYS_ARR[@]} -eq 0 ]; then
                    warn "Minimal 1 key wajib diisi."
                    continue
                fi
                break
            fi
            if command -v curl &>/dev/null; then
                if validate_odds_api_key "$ONE_KEY"; then
                    ODDS_KEYS_ARR+=("$ONE_KEY")
                else
                    warn "Key ini ditolak — tidak ditambahkan ke daftar rotasi. Coba key lain atau Enter untuk selesai."
                fi
            else
                ODDS_KEYS_ARR+=("$ONE_KEY")
            fi
        done
        JOINED=$(IFS=,; echo "${ODDS_KEYS_ARR[*]}")
        set_env_value "ODDS_API_KEYS" "$JOINED"
        set_env_value "ODDS_API_KEY" "${ODDS_KEYS_ARR[0]}"
        ok "${#ODDS_KEYS_ARR[@]} key disimpan ke ODDS_API_KEYS (rotasi aktif)."
    else
        prompt_env_key "ODDS_API_KEY" "API key data odds (wajib) — daftar gratis di https://the-odds-api.com" "required"
        if command -v curl &>/dev/null; then
            ODDS_KEY_VAL=$(get_env_value "ODDS_API_KEY")
            if [ -n "$ODDS_KEY_VAL" ]; then
                info "Memvalidasi ODDS_API_KEY ke the-odds-api.com..."
                while ! validate_odds_api_key "$ODDS_KEY_VAL"; do
                    if [ ! -t 0 ]; then
                        fail "ODDS_API_KEY tidak valid dan tidak ada input interaktif untuk perbaikan."
                    fi
                    read -rp "  Masukkan ulang ODDS_API_KEY yang benar: " ODDS_KEY_VAL
                    ODDS_KEY_VAL=$(echo "$ODDS_KEY_VAL" | tr -d '[:space:]')
                    [ -z "$ODDS_KEY_VAL" ] && fail "ODDS_API_KEY wajib diisi."
                    set_env_value "ODDS_API_KEY" "$ODDS_KEY_VAL"
                done
            fi
        else
            warn "curl tidak ditemukan — validasi langsung ODDS_API_KEY dilewati."
        fi
    fi
else
    fail "ODDS_API_KEY/ODDS_API_KEYS belum diisi di .env dan tidak ada input interaktif tersedia."
fi

prompt_env_key "TELEGRAM_BOT_TOKEN" "Token bot Telegram (opsional, untuk alert) — dari @BotFather" "optional"
prompt_env_key "TELEGRAM_CHAT_ID" "Chat ID Telegram (opsional, untuk alert) — dari @userinfobot" "optional"

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
if not os.environ.get("ODDS_API_KEY", "").strip() and not os.environ.get("ODDS_API_KEYS", "").strip():
    missing.append("ODDS_API_KEY/ODDS_API_KEYS")

for k in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
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
