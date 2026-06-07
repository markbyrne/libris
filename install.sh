#!/usr/bin/env bash
# Libris installer — sets up system dependencies, installs the package,
# and walks you through creating a config file.
#
# Run from the root of the libris repository:
#   bash install.sh

set -uo pipefail
IFS=$'\n\t'

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

info()    { echo -e "  ${BLUE}→${NC}  $*"; }
success() { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${NC}  $*" >&2; }
error()   { echo -e "  ${RED}✗${NC}  $*" >&2; }
header()  { echo -e "\n${BOLD}$*${NC}"; }
hr()      { echo -e "${DIM}────────────────────────────────────────────────${NC}"; }

# ── Prompt helpers ─────────────────────────────────────────────────────────────
ask() {
    # ask "Prompt" "default" → prints default in brackets; empty input → default
    local prompt="$1" default="${2:-}" value
    if [[ -n "$default" ]]; then
        printf "  %s [%s]: " "$prompt" "$default"
    else
        printf "  %s: " "$prompt"
    fi
    read -r value
    printf '%s' "${value:-$default}"
}

ask_secret() {
    # Like ask but hides input (for API keys / tokens)
    local prompt="$1" value
    printf "  %s (hidden, Enter to skip): " "$prompt"
    read -rs value; echo ""
    printf '%s' "$value"
}

ask_yn() {
    # ask_yn "Question" "y"|"n" → returns 0 (yes) or 1 (no)
    local prompt="$1" default="${2:-y}" value
    local yes_label no_label
    [[ "$default" == "y" ]] && yes_label="Y" || yes_label="y"
    [[ "$default" == "n" ]] && no_label="N"  || no_label="n"
    printf "  %s [%s/%s]: " "$prompt" "$yes_label" "$no_label"
    read -r value
    value="${value:-$default}"
    [[ "$value" =~ ^[Yy] ]]
}

# ── Platform detection ────────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM=macos ;;
    Linux)  PLATFORM=linux ;;
    *)
        error "Unsupported platform: $OS. Libris runs on macOS and Linux."
        exit 1
        ;;
esac

# ── Must run from the repo root ───────────────────────────────────────────────
if [[ ! -f "pyproject.toml" ]] || ! grep -q 'name = "libris"' pyproject.toml 2>/dev/null; then
    error "Run this script from the root of the libris repository:"
    error "  cd /path/to/libris && bash install.sh"
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Libris installer${NC}  —  intelligent Calibre book importer"
hr
echo ""
echo "  This script will:"
echo "    1. Check and install system dependencies"
echo "    2. Install the libris Python package"
echo "    3. Create a config file"
echo "    4. Add LIBRIS_CONFIG to your shell profile"
echo "    5. Optionally install a daemon service"
echo "    6. Verify everything works"
echo ""
if ! ask_yn "Continue?" "y"; then
    echo "  Cancelled."; exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
header "1 · System dependencies"
hr

check_cmd() {
    if command -v "$1" &>/dev/null; then
        success "$1  ($(command -v "$1"))"
        return 0
    fi
    return 1
}

# Python ≥ 3.10
if ! check_cmd python3; then
    error "python3 not found. Install Python 3.10+ from https://python.org and re-run."
    exit 1
fi
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if (( PY_MAJOR < 3 || (PY_MAJOR == 3 && PY_MINOR < 10) )); then
    error "Python 3.10+ required. Found: $PY_VER"
    exit 1
fi
success "Python $PY_VER"

# pip
PIP_CMD=""
for _cmd in pip3 pip "python3 -m pip"; do
    if $_cmd --version &>/dev/null 2>&1; then
        PIP_CMD="$_cmd"
        break
    fi
done
if [[ -z "$PIP_CMD" ]]; then
    error "pip not found. Try: python3 -m ensurepip --upgrade"
    exit 1
fi
success "pip  ($PIP_CMD)"

# Platform-specific deps
if [[ "$PLATFORM" == "macos" ]]; then
    HAS_BREW=false
    command -v brew &>/dev/null && HAS_BREW=true

    for tool in fswatch ffmpeg calibredb; do
        if ! check_cmd "$tool"; then
            if $HAS_BREW; then
                # calibre is installed as an app bundle, not via brew formulae
                if [[ "$tool" == "calibredb" ]]; then
                    warn "calibredb not found. Install Calibre from https://calibre-ebook.com/download_osx"
                    warn "Make sure /Applications/calibre.app/Contents/MacOS is in your PATH."
                else
                    if ask_yn "Install $tool via Homebrew?" "y"; then
                        brew install "$tool"
                    fi
                fi
            else
                warn "$tool not found. Install it (and Homebrew from https://brew.sh) then re-run."
            fi
        fi
    done

elif [[ "$PLATFORM" == "linux" ]]; then
    MISSING=()
    check_cmd inotifywait || MISSING+=("inotify-tools")
    check_cmd ffmpeg       || MISSING+=("ffmpeg")
    if ! check_cmd calibredb; then
        warn "calibredb not found. Install Calibre from https://calibre-ebook.com/download_linux"
    fi

    if [[ ${#MISSING[@]} -gt 0 ]]; then
        warn "Missing packages: ${MISSING[*]}"
        if command -v apt-get &>/dev/null && ask_yn "Install them now via apt-get?" "y"; then
            sudo apt-get update -qq
            sudo apt-get install -y "${MISSING[@]}"
        elif command -v dnf &>/dev/null && ask_yn "Install them now via dnf?" "y"; then
            sudo dnf install -y "${MISSING[@]}"
        else
            warn "Install manually and re-run."
        fi
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
header "2 · Install Libris"
hr

echo ""
info "Installing libris from current directory…"
if ! $PIP_CMD install --quiet .; then
    error "pip install failed — check the output above for details."
    error "Common causes: wrong Python environment, missing build tools, or network error."
    exit 1
fi
if command -v libris &>/dev/null; then
    LIBRIS_BIN="$(command -v libris)"
    success "libris installed  ($LIBRIS_BIN)"
else
    # pip --user installs land in a non-default location on some systems
    USER_BIN="$(python3 -m site --user-base 2>/dev/null)/bin"
    if [[ -f "$USER_BIN/libris" ]]; then
        LIBRIS_BIN="$USER_BIN/libris"
        warn "libris installed to $LIBRIS_BIN but that directory is not on PATH."
        warn "Add this to your shell profile and re-run:"
        warn "  export PATH=\"\$PATH:$USER_BIN\""
    else
        LIBRIS_BIN="libris"   # best-effort; check-config will catch it
        warn "libris binary not found — PATH may need updating."
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
header "3 · Config file"
hr

echo ""
echo "  Press Enter at any prompt to accept the default [shown in brackets]."
echo ""

# Config path
CONFIG_DEFAULT="$HOME/.config/libris/config.yaml"
CONFIG_PATH="$(ask "Config file path" "$CONFIG_DEFAULT")"
CONFIG_PATH="${CONFIG_PATH/#\~/$HOME}"

SKIP_CONFIG=false
if [[ -f "$CONFIG_PATH" ]]; then
    echo ""
    warn "A config file already exists at $CONFIG_PATH"
    if ! ask_yn "Overwrite it?" "n"; then
        info "Keeping existing config."
        SKIP_CONFIG=true
    fi
fi

if [[ "$SKIP_CONFIG" == false ]]; then
    echo ""

    # ── Book directories ───────────────────────────────────────────────────────
    echo -e "  ${BOLD}Book directories:${NC}"
    BOOKS_ROOT="$(ask "Root directory for book folders" "$HOME/books")"
    BOOKS_ROOT="${BOOKS_ROOT/#\~/$HOME}"
    echo ""
    INCOMING="$(ask "  Incoming dir  (drop files here)" "$BOOKS_ROOT/incoming")"
    STAGING="$( ask "  Staging dir   (conversion workspace)" "$BOOKS_ROOT/staging")"
    REVIEW="$(  ask "  Review dir    (low-confidence matches)" "$BOOKS_ROOT/review")"
    FAILED="$(  ask "  Failed dir    (processing errors)" "$BOOKS_ROOT/failed")"
    STATE_DB="$(ask "  State DB path" "$BOOKS_ROOT/libris.db")"
    INCOMING="${INCOMING/#\~/$HOME}"; STAGING="${STAGING/#\~/$HOME}"
    REVIEW="${REVIEW/#\~/$HOME}";    FAILED="${FAILED/#\~/$HOME}"
    STATE_DB="${STATE_DB/#\~/$HOME}"

    echo ""

    # ── Calibre ───────────────────────────────────────────────────────────────
    echo -e "  ${BOLD}Calibre:${NC}"
    CALIBRE_MODE="$(ask "Mode (local or docker)" "local")"

    if [[ "$CALIBRE_MODE" == "docker" ]]; then
        DOCKER_CONTAINER="$(ask "Docker container name" "calibre-web")"
        LIBRARY_PATH=""
        CALIBRE_YAML="calibre:
  mode: docker
  docker_container: $DOCKER_CONTAINER"
    else
        CALIBRE_MODE="local"
        LIBRARY_PATH="$(ask "Calibre library path" "$HOME/Calibre Library")"
        LIBRARY_PATH="${LIBRARY_PATH/#\~/$HOME}"
        DOCKER_CONTAINER=""
        CALIBRE_YAML="calibre:
  mode: local
  library_path: $LIBRARY_PATH"
    fi

    echo ""

    # ── Metadata ──────────────────────────────────────────────────────────────
    echo -e "  ${BOLD}Metadata:${NC}"
    THRESHOLD="$(ask "Confidence threshold (0.0–1.0, default is good)" "0.75")"
    echo ""
    echo "  A Google Books API key gives 1,000 requests/day (vs. ~60/min unauthenticated)."
    echo "  Get one free at: https://console.developers.google.com/"
    GOOGLE_KEY="$(ask_secret "Google Books API key")"
    if [[ -n "$GOOGLE_KEY" ]]; then
        GOOGLE_KEY_YAML="  google_books_api_key: $GOOGLE_KEY"
    else
        GOOGLE_KEY_YAML="  # google_books_api_key: YOUR_KEY_HERE"
    fi

    echo ""

    # ── ntfy notifications ────────────────────────────────────────────────────
    echo -e "  ${BOLD}Notifications (ntfy.sh — optional):${NC}"
    echo "  Sends push alerts to your phone when files need review."
    echo "  Free at https://ntfy.sh — no account required for public topics."
    echo ""
    NTFY_ENABLED="false"
    NTFY_TOPIC="my-libris"
    NTFY_AUTH_YAML="  # auth_token: YOUR_TOKEN_HERE"

    if ask_yn "Enable ntfy notifications?" "n"; then
        RAND_SUFFIX="$(openssl rand -hex 3 2>/dev/null || od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
        NTFY_TOPIC="$(ask "ntfy topic name (make it unguessable)" "libris-$RAND_SUFFIX")"
        NTFY_ENABLED="true"
        NTFY_AUTH="$(ask_secret "ntfy auth token (for private topics)")"
        if [[ -n "$NTFY_AUTH" ]]; then
            NTFY_AUTH_YAML="  auth_token: $NTFY_AUTH"
        fi
    fi

    echo ""

    # ── Log level ─────────────────────────────────────────────────────────────
    echo -e "  ${BOLD}Logging:${NC}"
    LOG_LEVEL="$(ask "Log level (DEBUG / INFO / WARNING)" "INFO")"

    # ── Create directories ────────────────────────────────────────────────────
    echo ""
    info "Creating directories…"
    mkdir -p "$INCOMING" "$STAGING" "$STAGING/pending" "$REVIEW" "$FAILED" \
             "$(dirname "$STATE_DB")" "$(dirname "$CONFIG_PATH")"
    success "Directories ready"

    # ── Write config ──────────────────────────────────────────────────────────
    cat > "$CONFIG_PATH" <<YAML
# Libris configuration — generated by install.sh $(date +%Y-%m-%d)
# Edit this file to adjust settings, then run: libris check-config
#
# Full documentation: https://github.com/markbyrne/libris#readme

watcher:
  incoming_dir: $INCOMING
  scan_interval_hours: 1.0     # re-scan on startup + every N hours (0 = startup only)

paths:
  staging_dir: $STAGING
  review_dir:  $REVIEW
  failed_dir:  $FAILED
  state_db:    $STATE_DB

$CALIBRE_YAML

metadata:
  confidence_threshold: $THRESHOLD
$GOOGLE_KEY_YAML
  overwrite_existing: true
  mock_mode: false
  duplicate_action: review     # review | skip | import

output:
  preferred_ebook_format: epub    # epub | mobi
  ebook_format_policy: preferred  # preferred | all
  embed_cover_art: true

ntfy:
  topic: $NTFY_TOPIC
  base_url: https://ntfy.sh
  enabled: $NTFY_ENABLED
$NTFY_AUTH_YAML

log_level: $LOG_LEVEL   # DEBUG | INFO | WARNING | ERROR
YAML

    success "Config written to $CONFIG_PATH"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "4 · Shell profile"
hr

echo ""

# Detect which profile file to use
if [[ "$(basename "${SHELL:-bash}")" == "zsh" ]]; then
    PROFILE="$HOME/.zshrc"
elif [[ "$PLATFORM" == "macos" ]]; then
    PROFILE="$HOME/.bash_profile"
else
    PROFILE="$HOME/.bashrc"
fi

EXPORT_LINE="export LIBRIS_CONFIG=\"$CONFIG_PATH\""

if grep -qF "LIBRIS_CONFIG" "$PROFILE" 2>/dev/null; then
    success "LIBRIS_CONFIG already set in $PROFILE"
else
    echo "  libris looks for LIBRIS_CONFIG to find your config file from any directory."
    echo ""
    if ask_yn "Add 'export LIBRIS_CONFIG' to $PROFILE?" "y"; then
        { echo ""; echo "# Libris config"; echo "$EXPORT_LINE"; } >> "$PROFILE"
        success "Added to $PROFILE"
        info "Run: source $PROFILE   (or open a new terminal)"
    else
        echo ""
        info "Add this line to your shell profile manually:"
        echo ""
        echo "    $EXPORT_LINE"
        echo ""
    fi
fi

# Make it available in this session too
export LIBRIS_CONFIG="$CONFIG_PATH"

# ─────────────────────────────────────────────────────────────────────────────
header "5 · Run as a daemon (optional)"
hr

echo ""
echo "  Running 'libris run' as a background service starts the watcher automatically"
echo "  on login and keeps it running if it crashes."
echo ""

if [[ "$PLATFORM" == "macos" ]]; then
    PLIST="$HOME/Library/LaunchAgents/com.libris.plist"
    LOG_DIR="$HOME/Library/Logs/libris"

    if ask_yn "Install a LaunchAgent (auto-start on login)?" "n"; then
        mkdir -p "$LOG_DIR"
        cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>              <string>com.libris</string>
    <key>ProgramArguments</key>
    <array>
        <string>$LIBRIS_BIN</string>
        <string>run</string>
        <string>--config</string>
        <string>$CONFIG_PATH</string>
    </array>
    <key>RunAtLoad</key>          <true/>
    <key>KeepAlive</key>          <true/>
    <key>StandardOutPath</key>    <string>$LOG_DIR/libris.log</string>
    <key>StandardErrorPath</key>  <string>$LOG_DIR/libris.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LIBRIS_CONFIG</key>  <string>$CONFIG_PATH</string>
    </dict>
</dict>
</plist>
PLIST
        if launchctl load "$PLIST" 2>/dev/null; then
            success "LaunchAgent installed and started"
        else
            warn "LaunchAgent written but not loaded — load it manually:"
            warn "  launchctl load $PLIST"
        fi
        info "Logs:   $LOG_DIR/libris.log"
        info "Stop:   launchctl unload $PLIST"
        info "Start:  launchctl load $PLIST"
    fi

elif [[ "$PLATFORM" == "linux" ]]; then
    UNIT_DIR="$HOME/.config/systemd/user"
    UNIT="$UNIT_DIR/libris.service"

    if ask_yn "Install a systemd user service (auto-start on login)?" "n"; then
        mkdir -p "$UNIT_DIR"
        cat > "$UNIT" <<UNIT
[Unit]
Description=Libris book importer daemon
After=network.target

[Service]
Type=simple
ExecStart=$LIBRIS_BIN run --config $CONFIG_PATH
Restart=on-failure
RestartSec=10
Environment=LIBRIS_CONFIG=$CONFIG_PATH

[Install]
WantedBy=default.target
UNIT
        if systemctl --user daemon-reload 2>/dev/null && \
           systemctl --user enable --now libris.service 2>/dev/null; then
            success "systemd user service installed and started"
        else
            warn "Service file written but could not be enabled. Try:"
            warn "  systemctl --user daemon-reload"
            warn "  systemctl --user enable --now libris"
        fi
        info "Logs:    journalctl --user -u libris -f"
        info "Status:  systemctl --user status libris"
        info "Stop:    systemctl --user stop libris"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
header "6 · Verify"
hr

echo ""
info "Running libris check-config…"
echo ""

if "$LIBRIS_BIN" check-config 2>/dev/null || libris check-config 2>/dev/null; then
    echo ""
    success "Setup complete."
else
    echo ""
    warn "check-config reported issues — review $CONFIG_PATH and re-run:"
    warn "  libris check-config"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
hr
echo ""
echo -e "  ${BOLD}You're ready. Next steps:${NC}"
echo ""
echo "  Test a single import:"
echo "    libris import-one ~/path/to/book.epub"
echo ""
echo "  Start the watcher daemon:"
echo "    libris run"
echo ""
echo "  Review low-confidence matches:"
echo "    libris list-review"
echo ""
echo "  Full docs:  https://github.com/markbyrne/libris#readme"
echo ""
