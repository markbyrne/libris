#!/usr/bin/env bash
# Libris installer — sets up system dependencies, installs the package,
# and walks you through creating a config file.
#
# Run from the root of the libris repository:
#   bash install.sh

set -uo pipefail
IFS=$'\n\t'

# ── Debug flag (pass --debug to enable) ───────────────────────────────────────
DEBUG=false
for _arg in "${@:-}"; do
    [[ "$_arg" == "--debug" ]] && DEBUG=true
done
debug() { $DEBUG && echo -e "  [debug] $*" >&2 || true; }

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
    read -r value < /dev/tty
    printf '%s' "${value:-$default}"
}

ask_secret() {
    # Like ask but hides input (for API keys / tokens)
    local prompt="$1" value
    printf "  %s (hidden, Enter to skip): " "$prompt"
    read -rs value < /dev/tty; echo ""
    printf '%s' "$value"
}

ask_yn() {
    # ask_yn "Question" "y"|"n" → returns 0 (yes) or 1 (no)
    local prompt="$1" default="${2:-y}" value
    local yes_label no_label
    [[ "$default" == "y" ]] && yes_label="Y" || yes_label="y"
    [[ "$default" == "n" ]] && no_label="N"  || no_label="n"
    printf "  %s [%s/%s]: " "$prompt" "$yes_label" "$no_label"
    read -r value < /dev/tty
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

# ── Detect local vs remote install mode ──────────────────────────────────────
LIBRIS_VERSION="v0.2.0-beta"
LIBRIS_REPO="https://github.com/markbyrne/libris"

if [[ -f "pyproject.toml" ]] && grep -q 'name = "libris"' pyproject.toml 2>/dev/null; then
    INSTALL_MODE="local"
else
    INSTALL_MODE="remote"
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

    # python3-venv is required for the externally-managed Python install path
    if ! python3 -m venv --help &>/dev/null 2>&1; then
        MISSING+=("python3-venv")
    fi

    if [[ ${#MISSING[@]} -gt 0 ]]; then
        warn "Missing packages: ${MISSING[*]}"
        if command -v apt-get &>/dev/null; then
            info "Installing missing packages via apt-get…"
            sudo apt-get update -qq
            sudo apt-get install -y "${MISSING[@]}"
        elif command -v dnf &>/dev/null; then
            info "Installing missing packages via dnf…"
            sudo dnf install -y "${MISSING[@]}"
        else
            warn "Install manually and re-run: ${MISSING[*]}"
        fi
    fi

    if ! check_cmd calibredb; then
        warn "calibredb not found."
        echo ""
        echo "  Calibre is not available via apt — it must be installed from calibre-ebook.com."
        if ask_yn "Install Calibre now? (uses the official Calibre installer)" "y"; then
            # Install known Calibre prerequisites on Debian/Ubuntu
            if command -v apt-get &>/dev/null; then
                info "Installing Calibre prerequisites…"
                sudo apt-get install -y \
                    libxcb-cursor0 libgl1 libegl1 \
                    libxkbcommon0 libdbus-1-3 2>/dev/null || true
            fi
            if command -v wget &>/dev/null; then
                sudo -v && wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sudo sh /dev/stdin
            elif command -v curl &>/dev/null; then
                sudo -v && curl -fsSL https://download.calibre-ebook.com/linux-installer.sh | sudo sh /dev/stdin
            else
                warn "Neither wget nor curl found — install Calibre manually from https://calibre-ebook.com/download_linux"
            fi
            # Add Calibre to PATH for the rest of this session
            export PATH="$PATH:/opt/calibre"
            if check_cmd calibredb; then
                success "Calibre installed"
            else
                warn "calibredb still not found — you may need to add Calibre to PATH."
                warn "Typically: export PATH=\"\$PATH:/opt/calibre\""
            fi
        else
            warn "Skipping Calibre install. Add calibredb to PATH before running libris."
        fi
        echo ""
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
header "2 · Install Libris"
hr

echo ""
if [[ "$INSTALL_MODE" == "local" ]]; then
    info "Installing libris from local source…"
    INSTALL_TARGET="."
else
    # ── Private repo token check ──────────────────────────────────────────────
    # This block is only needed while libris is a private repo during alpha/beta
    # testing. Once the repo is made public, this check will always pass and the
    # token prompt will never appear. It can be removed at that point.
    GITHUB_TOKEN=""
    REPO_URL="https://api.github.com/repos/markbyrne/libris"
    if ! curl -fsSL "$REPO_URL" &>/dev/null; then
        echo ""
        warn "The libris repository is private — a GitHub token is required to install."
        echo "  Generate one at: GitHub → Settings → Developer settings → Personal access tokens"
        echo "  Required scope: repo (read) — or use a fine-grained token with Contents: read"
        echo ""
        GITHUB_TOKEN="$(ask_secret "GitHub personal access token")"
        if [[ -z "$GITHUB_TOKEN" ]]; then
            error "No token provided — cannot install from private repo."
            exit 1
        fi
    fi

    info "Installing libris ${LIBRIS_VERSION} from GitHub…"

    if [[ -n "$GITHUB_TOKEN" ]]; then
        # Private repo: download the tarball via GitHub API (avoids git credential
        # URL issues) then install from the local file. This block is only needed
        # during private alpha/beta testing — remove once the repo is public.
        #
        # NOTE: pasted PATs frequently carry a trailing CR (\r) or surrounding
        # whitespace, especially over SSH/PuTTY. With IFS=$'\n\t', `read` does not
        # strip a trailing CR, so the token can end up as "ghp_xxx\r". Passing that
        # inside an "Authorization:" header makes older libcurl (Debian/Ubuntu)
        # reject it with curl error 43 (CURLE_BAD_FUNCTION_ARGUMENT). Strip all
        # CR / whitespace first, then hand curl the header via a --config file on
        # stdin so the token never lands in the process list or the command line.
        GITHUB_TOKEN="$(printf '%s' "$GITHUB_TOKEN" | tr -d '[:space:]')"
        if [[ -z "$GITHUB_TOKEN" ]]; then
            error "GitHub token was empty after trimming whitespace — re-run and paste it again."
            exit 1
        fi
        debug "token length after trim: ${#GITHUB_TOKEN} chars"
        debug "curl version: $(curl --version 2>&1 | head -1)"
        debug "git version:  $(git --version 2>&1)"

        LIBRIS_TARBALL="/tmp/libris-${LIBRIS_VERSION}.tar.gz"
        LIBRIS_API_URL="https://api.github.com/repos/markbyrne/libris/tarball/${LIBRIS_VERSION}"
        debug "tarball path: $LIBRIS_TARBALL"
        debug "api url:      $LIBRIS_API_URL"
        trap 'rm -f "$LIBRIS_TARBALL"' EXIT

        download_ok=false
        if command -v curl &>/dev/null; then
            debug "attempting curl download via --config stdin…"
            CURL_CONFIG="$(printf 'header = "Authorization: token %s"\n' "$GITHUB_TOKEN")"
            debug "curl config line: header = \"Authorization: token [REDACTED]\""
            CURL_EXIT=0
            printf '%s\n' "$CURL_CONFIG" \
                | curl --fail --silent --show-error --location \
                       --config - \
                       --output "$LIBRIS_TARBALL" \
                       "$LIBRIS_API_URL" || CURL_EXIT=$?
            debug "curl exit code: $CURL_EXIT"
            if [[ $CURL_EXIT -eq 0 ]]; then
                download_ok=true
                debug "tarball size: $(wc -c < "$LIBRIS_TARBALL") bytes"
            else
                debug "curl failed — trying verbose mode for diagnostics…"
                $DEBUG && printf '%s\n' "$CURL_CONFIG" \
                    | curl --location --config - \
                           --output "$LIBRIS_TARBALL" \
                           --verbose \
                           "$LIBRIS_API_URL" 2>&1 | grep -E "^[<>*]" >&2 || true
            fi
        fi

        if [[ "$download_ok" != true ]] && command -v wget &>/dev/null; then
            warn "curl download failed — retrying with wget…"
            debug "attempting wget download…"
            WGET_EXIT=0
            wget --quiet \
                 --header="Authorization: token ${GITHUB_TOKEN}" \
                 -O "$LIBRIS_TARBALL" \
                 "$LIBRIS_API_URL" || WGET_EXIT=$?
            debug "wget exit code: $WGET_EXIT"
            [[ $WGET_EXIT -eq 0 ]] && download_ok=true
        fi

        if [[ "$download_ok" != true ]]; then
            error "Failed to download libris tarball — check your token and try again."
            rm -f "$LIBRIS_TARBALL"
            exit 1
        fi

        INSTALL_TARGET="$LIBRIS_TARBALL"
    else
        INSTALL_TARGET="git+${LIBRIS_REPO}@${LIBRIS_VERSION}"
    fi
fi

# Detect externally-managed Python (PEP 668 — Debian/Ubuntu 23.04+)
# The marker lives in the stdlib dir, not sys.prefix root.
VENV_DIR="$HOME/.local/share/libris/venv"
EXTERNALLY_MANAGED=false
if python3 -c "
import sysconfig, os, sys
stdlib = sysconfig.get_path('stdlib')
if stdlib and os.path.exists(os.path.join(stdlib, 'EXTERNALLY-MANAGED')):
    sys.exit(0)
if os.path.exists(os.path.join(sys.base_prefix, 'EXTERNALLY-MANAGED')):
    sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
    EXTERNALLY_MANAGED=true
fi

if [[ "$EXTERNALLY_MANAGED" == true ]]; then
    info "System Python is externally managed — installing into a virtual environment…"
    info "Venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
    VENV_PIP="$VENV_DIR/bin/pip"
    VENV_LIBRIS="$VENV_DIR/bin/libris"
    if ! "$VENV_PIP" install --quiet "$INSTALL_TARGET"; then
        error "pip install into venv failed — check the output above for details."
        exit 1
    fi
    # Symlink into ~/.local/bin so it's on PATH
    mkdir -p "$HOME/.local/bin"
    ln -sf "$VENV_LIBRIS" "$HOME/.local/bin/libris"
    PIP_CMD="$VENV_PIP"
else
    if ! $PIP_CMD install --quiet "$INSTALL_TARGET"; then
        error "pip install failed — check the output above for details."
        error "Common causes: wrong Python environment, missing build tools, or network error."
        exit 1
    fi
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
