#!/bin/sh
# Athena installer — one command, and `athena` works in every directory.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/allchapters/Athena/main/install.sh)"
#
# In order: check that python3 is new enough, download a snapshot of the package,
# put it in ~/.local/share/athena, and write a small launcher to ~/.local/bin.
# It does not ask for an API key — the first run of `athena` does that, once, and
# saves it. Installing and configuring are different jobs, and a workshop that
# installs on twenty laptops should not have to answer twenty prompts.
#
# What it deliberately does not do: use pip, build a virtualenv, or edit a shell
# profile without asking. Athena has no dependencies, so there is nothing to
# resolve — PYTHONPATH is the entire mechanism, and it has no failure modes.
#
# Overridable: ATHENA_REF (branch or tag, default main), ATHENA_PREFIX, ATHENA_BIN.

set -eu

OWNER=allchapters
REPO=Athena
REF="${ATHENA_REF:-main}"

PREFIX="${ATHENA_PREFIX:-$HOME/.local/share}"
BIN="${ATHENA_BIN:-$HOME/.local/bin}"
PKG="$PREFIX/athena"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/athena/env"
LAUNCHER="$BIN/athena"

# 3.10 is a hard floor, not a preference: the package annotates with PEP 604
# unions (str | None) and those are evaluated when the module is imported, so an
# older python fails at `import athena` rather than somewhere a user could debug.
MIN_MAJOR=3
MIN_MINOR=10

say() { printf '%s\n' "$*"; }
step() { printf '  %s\n' "$*"; }
die() { printf 'athena: %s\n' "$*" >&2; exit 1; }

# Whether there is a human to ask. Not `[ -r /dev/tty ]`: that tests the device
# file, which is readable in a process that has no controlling terminal at all,
# and the write then fails with "Device not configured". Opening it is the only
# question worth asking, so ask it that way.
have_tty() { { true > /dev/tty; } 2>/dev/null; }

# ------------------------------------------------------------------- preflight

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"
}

say ""
say "Installing Athena — the smallest agent harness."
say ""

need curl
need tar

# Stock macOS still ships 3.9, so this branch is common enough to deserve the
# fix rather than just the diagnosis.
PYTHON_HELP="Install a newer one:  brew install python@3.12
  or download it from  https://www.python.org/downloads/"

PYTHON="$(command -v python3 2>/dev/null || true)"
[ -n "$PYTHON" ] || die "python3 is required.
  $PYTHON_HELP"
"$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)" \
    || die "python3 $MIN_MAJOR.$MIN_MINOR or newer is required ($("$PYTHON" -V 2>&1) found at $PYTHON).
  $PYTHON_HELP"
step "python  $("$PYTHON" -V 2>&1) at $PYTHON"

# Paths get baked into the launcher as single-quoted strings. A quote inside one
# would need escaping machinery that earns nothing here, so refuse it plainly.
case "$PKG$CFG$PYTHON" in
    *"'"*) die "install paths contain a single quote — set ATHENA_PREFIX to a simpler path" ;;
esac

# ----------------------------------------------------------------------- fetch

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

# Downloaded to a file rather than piped into tar: `curl | tar` hides curl's exit
# status behind tar's unless the shell has pipefail, which POSIX sh does not.
fetch() {
    curl -fsSL "https://codeload.github.com/$OWNER/$REPO/tar.gz/$1/$REF" \
        -o "$TMP/athena.tar.gz" 2>/dev/null
}

if fetch refs/heads; then
    step "source  $REF (branch)"
elif fetch refs/tags; then
    step "source  $REF (tag)"
elif command -v git >/dev/null 2>&1 && \
     git clone --quiet --depth 1 --branch "$REF" \
        "https://github.com/$OWNER/$REPO.git" "$TMP/$REPO-git" 2>/dev/null; then
    step "source  $REF (git clone)"
    SRC="$TMP/$REPO-git"
else
    die "could not download $OWNER/$REPO at $REF — check the ref and your network"
fi

if [ -z "${SRC:-}" ]; then
    tar xzf "$TMP/athena.tar.gz" -C "$TMP" || die "the downloaded archive is not readable"
    # GitHub names the directory <repo>-<ref>, and a ref can contain a slash.
    SRC="$(find "$TMP" -maxdepth 1 -type d -name "$REPO-*" | head -1)"
fi

# Trust nothing about the archive: check for the two files that make it Athena.
[ -n "$SRC" ] && [ -f "$SRC/athena/__main__.py" ] && [ -f "$SRC/athena/harness.py" ] \
    || die "the download does not look like Athena — nothing was installed"
step "package $(ls "$SRC/athena"/*.py | wc -l | tr -d ' ') files"

# --------------------------------------------------------------------- install

# Move the new tree in beside the old one and swap, so a failed download can
# never leave a half-replaced install where a working one used to be.
mkdir -p "$PREFIX" "$BIN"
rm -rf "$PKG.new" "$PKG.old"
mv "$SRC" "$PKG.new"
[ -d "$PKG" ] && mv "$PKG" "$PKG.old"
mv "$PKG.new" "$PKG"
rm -rf "$PKG.old"
step "installed to $PKG"

# ---------------------------------------------------------------- the launcher

# Written in two halves: a generated header holding the paths this run resolved,
# then a fixed body. The body is a quoted heredoc so that everything in it — $1,
# $HOME, the backslashes — reaches the file exactly as written.
{
    printf '%s\n' "#!/bin/sh"
    printf '%s\n' "# Athena launcher — generated by install.sh. Re-run the installer to update."
    printf "ATHENA_PKG='%s'\n" "$PKG"
    printf "ATHENA_CFG='%s'\n" "$CFG"
    printf "ATHENA_PYTHON='%s'\n" "$PYTHON"
    cat <<'LAUNCHER'

set -eu

# Exported before anything else can exec python — --help does, and finding the
# package must not depend on which directory the user happens to be standing in.
# Prepended, not replaced: a user with their own PYTHONPATH keeps it.
PYTHONPATH="$ATHENA_PKG${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

# The key lives in one file, sourced here, so the harness itself needs no idea
# that a config file exists — provider.py reads the environment, as it always has.
[ -f "$ATHENA_CFG" ] && . "$ATHENA_CFG"

ask_for_key() {
    # /dev/tty rather than stdin: this runs with stdin pointed at whatever the
    # user piped in, and a key prompt that silently reads a task file is worse
    # than one that refuses. Testing it by opening it, because -r passes in a
    # process that has no controlling terminal and the write then fails.
    if ! { true > /dev/tty; } 2>/dev/null; then
        printf 'athena: no API key, and no terminal to ask on.\n' >&2
        printf '  export ATHENA_API_KEY=... and try again.\n' >&2
        exit 1
    fi

    cat > /dev/tty <<'INTRO'

Athena needs a Gemini API key to think with.
Get one, free, at: https://aistudio.google.com/apikey

It is checked against Google before being saved, stored readable only by you,
and asked for once. `athena --set-key` replaces it later.
INTRO

    # Echo off while a secret is typed, and restored even on Ctrl-C — a terminal
    # left with echo disabled looks like a hung machine.
    trap 'stty echo 2>/dev/null </dev/tty || true' INT TERM
    attempt=1
    while [ "$attempt" -le 3 ]; do
        printf '\nGemini API key: ' > /dev/tty
        stty -echo 2>/dev/null < /dev/tty || true
        IFS= read -r key < /dev/tty || key=''
        stty echo 2>/dev/null < /dev/tty || true
        printf '\n' > /dev/tty

        # Trim what a paste brings with it, then check the shape. This doubles as
        # the reason nothing below needs shell quoting: a real key is letters,
        # digits, dot, dash and underscore, and anything else is not one.
        key=$(printf '%s' "$key" | tr -d ' \011\015\012')
        attempt=$((attempt + 1))
        if [ -z "$key" ]; then
            printf '  nothing entered.\n' > /dev/tty
            continue
        fi
        case "$key" in
            *[!A-Za-z0-9._-]*)
                printf '  that contains characters no API key has — mistyped?\n' > /dev/tty
                continue ;;
        esac

        # Listing models proves the key works and spends no tokens.
        printf '  checking with Google... ' > /dev/tty
        code=$(curl -s -o /dev/null -w '%{http_code}' \
            "https://generativelanguage.googleapis.com/v1beta/models?key=$key" \
            2>/dev/null || printf '000')
        case "$code" in
            200)
                umask 077
                mkdir -p "$(dirname "$ATHENA_CFG")"
                printf 'export ATHENA_API_KEY="%s"\n' "$key" > "$ATHENA_CFG"
                chmod 600 "$ATHENA_CFG" 2>/dev/null || true
                printf 'works.\n  saved to %s\n' "$ATHENA_CFG" > /dev/tty
                trap - INT TERM
                return 0 ;;
            400|401|403)
                printf 'refused (HTTP %s). That key is not valid.\n' "$code" > /dev/tty ;;
            000)
                printf 'could not reach Google. Check your network.\n' > /dev/tty ;;
            *)
                printf 'unexpected HTTP %s.\n' "$code" > /dev/tty ;;
        esac
    done
    printf 'athena: no working key after 3 tries.\n' >&2
    exit 1
}

# Flags the launcher owns, taken before python sees them so that argparse in
# cli.py stays exactly the flag list the course shipped.
case "${1:-}" in
    --set-key)
        rm -f "$ATHENA_CFG"
        unset ATHENA_API_KEY
        shift
        ;;
    --where)
        printf 'package   %s\n' "$ATHENA_PKG"
        printf 'launcher  %s\n' "$0"
        printf 'key       %s\n' "$ATHENA_CFG"
        printf 'python    %s\n' "$ATHENA_PYTHON"
        exit 0
        ;;
    -h|--help)
        cat <<'USAGE'
athena — the smallest agent harness

  athena                  start a session in the current directory
  athena -p "task"        run one task and exit
  athena --resume         continue the last session in this directory
  athena --set-key        replace the saved Gemini API key
  athena --where          print the install paths

The directory you run it in is the sandbox: Athena cannot read or write outside
it. Transcripts go to ./.athena/sessions, so --resume is per project.

USAGE
        exec "$ATHENA_PYTHON" -m athena --help
        ;;
esac

if [ -z "${ATHENA_API_KEY:-}" ] && [ -z "${GEMINI_API_KEY:-}" ]; then
    ask_for_key
    . "$ATHENA_CFG"
fi

exec "$ATHENA_PYTHON" -m athena "$@"
LAUNCHER
} > "$LAUNCHER.new"

chmod 755 "$LAUNCHER.new"
mv "$LAUNCHER.new" "$LAUNCHER"
step "launcher $LAUNCHER"

# --------------------------------------------------------------------- the PATH

on_path=no
case ":$PATH:" in
    *":$BIN:"*) on_path=yes ;;
esac

if [ "$on_path" = no ]; then
    case "$(basename "${SHELL:-sh}")" in
        zsh)  rc="$HOME/.zshrc" ;;
        bash) rc="$HOME/.bashrc" ;;
        *)    rc="" ;;
    esac

    say ""
    say "$BIN is not on your PATH, so the shell cannot find \`athena\` yet."
    line="export PATH=\"$BIN:\$PATH\""
    # An installer that edits a shell profile without being asked is an
    # installer nobody trusts twice. Ask, and take silence as no.
    if [ -f "$rc" ] && grep -qF "$line" "$rc" 2>/dev/null; then
        # Already added by a previous install that the user has not sourced yet.
        # Appending it again would be the installer treating its own past work as
        # someone else's problem.
        step "already in $rc — run:  . $rc"
    elif [ -n "$rc" ] && have_tty; then
        printf '  add %s to %s? [y/N] ' "$line" "$rc" > /dev/tty
        IFS= read -r answer < /dev/tty || answer=n
        case "$answer" in
            y|Y|yes|YES)
                printf '\n# added by the Athena installer\n%s\n' "$line" >> "$rc"
                step "added to $rc — run:  . $rc"
                ;;
            *)  step "left alone. Add this line yourself:"; step "$line" ;;
        esac
    else
        step "add this line to your shell profile:"
        step "$line"
    fi
fi

# ------------------------------------------------------------------------- done

say ""
say "Athena is installed."
say ""
say "  cd any-project && athena          start a session, sandboxed to that directory"
say "  athena -p \"fix the failing test\"  one task, no prompt"
say "  athena --resume                   continue where you left off"
say ""
say "The first run asks for a Gemini API key and saves it to"
say "  $CFG"
say ""
say "  upgrade    re-run this installer"
say "  uninstall  rm -rf $PKG $LAUNCHER $(dirname "$CFG")"
say ""
