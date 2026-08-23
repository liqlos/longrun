#!/usr/bin/env bash
# longrun universal installer.
#
# Supported platforms: macOS (Homebrew), Debian/Ubuntu, Fedora/RHEL (dnf/yum).
# Installs the `longrun` CLI from this checkout as the single source of truth
# and wires the longrun skill into every agent CLI it finds: Claude Code,
# Codex, and OpenCode. Re-running is safe and idempotent.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
AGENTS_HOME="${LONGRUN_AGENTS_HOME:-$HOME/.agents}"
CANONICAL="$AGENTS_HOME/skills/longrun"

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARNING: %s\n' "$*"; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [ "$(id -u)" = "0" ]; then "$@"; else sudo "$@"; fi
}

# ---------------------------------------------------------------- python >= 3.11
python_ok() {
  [ -n "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if have "$cand" && python_ok "$cand"; then PY="$(command -v "$cand")"; break; fi
done

if [ -z "$PY" ]; then
  log "Python >= 3.11 not found; installing..."
  if have brew; then
    brew install python@3.12
  elif have apt-get; then
    as_root apt-get update -qq
    as_root DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip git
  elif have dnf; then
    as_root dnf install -y -q python3 python3-pip git
  elif have yum; then
    as_root yum install -y -q python3 python3-pip git
  else
    die "no supported package manager found (brew/apt-get/dnf/yum); install Python 3.11+ manually"
  fi
  for cand in python3.13 python3.12 python3.11 python3; do
    if have "$cand" && python_ok "$cand"; then PY="$(command -v "$cand")"; break; fi
  done
  python_ok "$PY" || die "Python >= 3.11 still unavailable after installation"
fi
log "using Python: $PY ($("$PY" --version))"

# ---------------------------------------------------------------- tool install
VENV="$HOME/.local/share/longrun/venv"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

if have uv; then
  log "installing longrun via uv (editable from $SRC)"
  uv tool install --editable --force "$SRC" >/dev/null
else
  log "uv not found; installing into a dedicated venv ($VENV)"
  "$PY" -m venv "$VENV" 2>/dev/null || { have apt-get && as_root apt-get install -y -qq python3-venv && "$PY" -m venv "$VENV"; }
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet --editable "$SRC"
  ln -sfn "$VENV/bin/longrun" "$BIN_DIR/longrun"
fi
have "$BIN_DIR/longrun" || have longrun || die "longrun binary was not installed"
log "installed: $("$BIN_DIR/longrun" --version 2>/dev/null || longrun --version)"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on PATH; add it:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

# ---------------------------------------------------------------- skill wiring
SKILL_SRC="$SRC/skills/longrun/SKILL.md"
[ -f "$SKILL_SRC" ] || die "skill source missing: $SKILL_SRC"
mkdir -p "$(dirname "$CANONICAL")"
if [ ! -f "$CANONICAL/SKILL.md" ] || ! cmp -s "$SKILL_SRC" "$CANONICAL/SKILL.md"; then
  mkdir -p "$CANONICAL"
  cp -f "$SKILL_SRC" "$CANONICAL/SKILL.md"
  log "canonical skill updated: $CANONICAL/SKILL.md"
fi

relative_link() { # target dir -> relative path string to CANONICAL
  "$PY" - "$1" "$CANONICAL" <<'EOF'
import os, sys
print(os.path.relpath(sys.argv[2], sys.argv[1]))
EOF
}

declare -a FOUND=() MISSED=()
wire_agent() { # binary, skills dir
  local bin="$1" dir="$2" rel link
  if have "$bin"; then
    mkdir -p "$dir"
    rel="$(relative_link "$dir")"
    link="$dir/longrun"
    if [ "$(readlink "$link" 2>/dev/null)" != "$rel" ]; then
      ln -sfn "$rel" "$link"
      log "wired $bin: $link -> $rel"
    else
      log "already wired $bin: $link"
    fi
    FOUND+=("$bin")
  else
    MISSED+=("$bin")
  fi
}

opencode_dir="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills"
wire_agent claude   "$HOME/.claude/skills"
wire_agent codex    "$HOME/.codex/skills"
wire_agent opencode "$opencode_dir"

# ---------------------------------------------------------------- summary
echo
log "harness : $( ("$BIN_DIR/longrun" --version 2>/dev/null || longrun --version) ) at $(have longrun && command -v longrun || echo "$BIN_DIR/longrun")"
log "source  : $SRC (single source of truth; updates = git pull + re-run)"
missed_note=""
[ "${#MISSED[@]}" -gt 0 ] && missed_note="; not found: ${MISSED[*]}"
log "agents  : detected ${#FOUND[@]} — ${FOUND[*]:-none}${missed_note}"
[ "${#FOUND[@]}" -eq 0 ] && warn "no agent CLI found among claude/codex/opencode; the harness works standalone via \`longrun go\`, but no agent skill was wired."
log "next    : longrun doctor   |   longrun models --driver <claude|codex|opencode>"
