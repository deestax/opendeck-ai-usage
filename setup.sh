#!/usr/bin/env bash
# Run from a checkout, or: bash <(curl -fsSL https://raw.githubusercontent.com/deestax/opendeck-ai-usage/main/setup.sh)
set -euo pipefail

main() {
    if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
        while IFS= read -r line; do printf '%s\n' "$line"; done <<'HELP'
OpenDeck AI Usage guided setup (native x86-64 Linux)
Usage: bash setup.sh [--dry-run] [--yes] [--replace] [--no-style]
                     [--install-udev] [--plugins-dir PATH]

Requires OpenDeck >=2.5, Git, Python >=3.11, Rust stable, a C linker and pgrep.
Installs Claude/Codex, missing Ajazz/OpenRouter plugins, and matching styling.
--dry-run       Check prerequisites and show the plan; no build/config writes.
--yes           Approve the printed user-level installation plan without prompts.
--replace       Also approve updating existing Claude/Codex plugins (backed up).
--no-style      Leave upstream display rendering untouched.
--install-udev  Separately authorize Ajazz udev rules via sudo (never automatic).
Existing upstream plugins, profiles and logins are preserved. No API keys asked for.
HELP
        return
    fi
    [[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || {
        echo 'Guided setup currently supports x86-64 Linux only.' >&2; return 1;
    }
    [[ $EUID -ne 0 ]] || { echo 'Run as your desktop user, NOT with sudo.' >&2; return 1; }
    command -v python3 >/dev/null || { echo 'Install Python 3.11+ first.' >&2; return 1; }
    command -v git >/dev/null || { echo 'Install Git first.' >&2; return 1; }
    # Cargo installed by rustup is sometimes absent from a newly opened shell.
    [[ -d "$HOME/.cargo/bin" ]] && export PATH="$HOME/.cargo/bin:$PATH"
    local source_dir
    source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    if [[ -f "$source_dir/scripts/setup.py" && -f "$source_dir/Cargo.lock" ]]; then
        python3 "$source_dir/scripts/setup.py" "$@"
    else
        # Fresh clone rather than git reset/pull in a user's working tree.
        local work cleanup_path
        work=$(mktemp -d -t opendeck-ai-usage.XXXXXXXX)
        printf -v cleanup_path '%q' "$work"
        trap "rm -rf -- $cleanup_path" EXIT
        echo 'Downloading OpenDeck AI Usage source from deestax/opendeck-ai-usage...'
        git clone --quiet --depth 1 https://github.com/deestax/opendeck-ai-usage.git "$work/source"
        echo "Source revision: $(git -C "$work/source" rev-parse HEAD)"
        python3 "$work/source/scripts/setup.py" "$@"
        rm -rf -- "$work"
        trap - EXIT
    fi
}
main "$@"
