#!/bin/bash
# SessionStart hook for Claude Code cloud sessions. Installs what
# .github/workflows/ci.yml installs, so `ruff`, `pytest`, `build`/`twine` and
# the customer-impact stamp run locally before a push. See CLAUDE.md.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

warn() { echo "session-start: $*" >&2; }
persist() {
  [ -n "${CLAUDE_ENV_FILE:-}" ] || return 0
  grep -qxF "$1" "$CLAUDE_ENV_FILE" 2>/dev/null || echo "$1" >> "$CLAUDE_ENV_FILE"
}

cd "$CLAUDE_PROJECT_DIR"

# --- Python environment -------------------------------------------------------
# CI: setup-python 3.13, `pip install -e ".[dev]"` (quality/test jobs) and
# `pip install build twine` (package job). The venv lives outside the checkout.
install_python() {
  local venv="$HOME/.cache/kanopy-python/venv" python=python3.13 stamp
  command -v "$python" >/dev/null || { warn "python3.13 not found; using python3 (CI quality job uses 3.13)"; python=python3; }
  # Reinstall when the interpreter, checkout path or package metadata changes.
  stamp=$({ "$python" --version; echo "$CLAUDE_PROJECT_DIR"; cat setup.cfg pyproject.toml; } | sha256sum | cut -d' ' -f1)
  if [ "$(cat "$venv/.deps-stamp" 2>/dev/null)" != "$stamp" ] || ! "$venv/bin/python" -c '' 2>/dev/null; then
    rm -rf "$venv"
    # Called under `||`, where `set -e` is off: chain every step with &&.
    "$python" -m venv "$venv" &&
      "$venv/bin/python" -m pip install --disable-pip-version-check -q -e ".[dev]" build twine &&
      echo "$stamp" > "$venv/.deps-stamp" || return 1
  fi
  persist "export VIRTUAL_ENV=\"$venv\""
  persist "export PATH=\"$venv/bin:\$PATH\""
}

# --- Release engine at the pinned commit -------------------------------------
# `.release-notes` entries are stamped and checked with the engine commit the
# workflows pin. Fetch exactly that commit outside the repository and export
# RELEASE_ENGINE so `python3 "$RELEASE_ENGINE/scripts/kanopy_release.py" stamp`
# and `check` validate the same way CI does.
install_release_engine() {
  local pins pin dest sibling line
  # Read both pin forms, like the engine's own `kanopy_release.py pin`:
  # `uses: KanopyAI/kanopy-release-engine@<sha>` and a checkout's
  # `repository: KanopyAI/kanopy-release-engine` followed by `ref: <sha>`.
  # `|| true`: grep finds nothing in repositories without a pin (pipefail).
  pins=$( { grep -rhoE 'KanopyAI/kanopy-release-engine(/[A-Za-z0-9._/-]+)?@[0-9a-f]{40}' \
      "$CLAUDE_PROJECT_DIR/.github/workflows" 2>/dev/null | sed 's/.*@//'
    grep -rh -A1 -E 'repository:[[:space:]]*KanopyAI/kanopy-release-engine[[:space:]]*$' \
      "$CLAUDE_PROJECT_DIR/.github/workflows" 2>/dev/null |
      sed -nE 's/^[[:space:]]*ref:[[:space:]]*([0-9a-f]{40})([^0-9a-f].*)?$/\1/p'
  } | sort -u) || true
  if [ -z "$pins" ]; then
    echo "session-start: no pinned release engine in .github/workflows" >&2
    return 0
  fi
  if [ "$(printf '%s\n' "$pins" | wc -l)" -ne 1 ]; then
    echo "session-start: workflows pin different release engines; skipping:" $pins >&2
    return 0
  fi
  pin=$pins
  dest="$HOME/.cache/kanopy-release-engine/$pin"
  if [ "$(git -C "$dest" rev-parse HEAD 2>/dev/null)" != "$pin" ]; then
    rm -rf "$dest"
    git init -q "$dest"
    # A sibling clone in multi-repo sessions avoids a network fetch.
    sibling="$CLAUDE_PROJECT_DIR/../kanopy-release-engine"
    if ! { [ -d "$sibling/.git" ] && git -C "$dest" fetch -q "$sibling" "$pin" 2>/dev/null; } &&
       ! git -C "$dest" fetch -q --depth 1 https://github.com/KanopyAI/kanopy-release-engine "$pin"; then
      rm -rf "$dest"
      echo "session-start: could not fetch KanopyAI/kanopy-release-engine@$pin." >&2
      echo "session-start: add that repository to the session to stamp .release-notes entries locally." >&2
      return 0
    fi
    git -C "$dest" -c advice.detachedHead=false checkout -q FETCH_HEAD
  fi
  local line="export RELEASE_ENGINE=\"$dest\""
  # The hook reruns on resume; write the export once.
  grep -qxF -- "$line" "${CLAUDE_ENV_FILE:-/dev/null}" 2>/dev/null ||
    echo "$line" >> "${CLAUDE_ENV_FILE:-/dev/null}"
}

# Stamps diff against origin/<base_branch>; make sure that ref exists.
fetch_release_base() {
  local base
  base=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_branch"])' \
    "$CLAUDE_PROJECT_DIR/.github/release-notes-source.json" 2>/dev/null) || return 0
  git -C "$CLAUDE_PROJECT_DIR" fetch -q origin "$base:refs/remotes/origin/$base" 2>/dev/null ||
    echo "session-start: could not fetch origin/$base" >&2
}

# A failed step warns and the session still starts.
install_python || warn "pip install -e \".[dev]\" build twine failed; ruff/pytest/build are unavailable"
install_release_engine || warn "release engine setup failed; stamp/check are unavailable"
fetch_release_base || true
