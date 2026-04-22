#!/usr/bin/env bash
# Translation catalog workflow. Run from repo root.
#
# Commands:
#   extract  - rescan sources into app/translations/messages.pot (run after any
#              user-facing string change in .py or .html templates).
#   update   - merge the fresh .pot into every locale's messages.po (adds new
#              msgids, marks removed ones obsolete). Review .po diffs, translate
#              any empty msgstr, then run compile.
#   compile  - turn .po into the binary .mo that Babel's Translations loads at
#              runtime. Commit the .mo alongside the .po.
#   init     - bootstrap a new locale. Usage: scripts/i18n.sh init <locale>
#              (POSIX form, e.g. ja, pt_BR, zh_Hans, zh_Hant).
#
# JSON catalogs under app/static/i18n/ are hand-authored; this script only
# touches the gettext side.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYBABEL="./venv/bin/pybabel"
CFG="babel.cfg"
POT="app/translations/messages.pot"
DIR="app/translations"

cmd="${1:-}"

case "$cmd" in
    extract)
        mkdir -p "$DIR"
        "$PYBABEL" extract -F "$CFG" -o "$POT" app/
        echo "wrote $POT"
        ;;
    update)
        "$PYBABEL" update -i "$POT" -d "$DIR"
        ;;
    compile)
        "$PYBABEL" compile -d "$DIR"
        ;;
    init)
        locale="${2:?usage: scripts/i18n.sh init <locale>}"
        "$PYBABEL" init -i "$POT" -d "$DIR" -l "$locale"
        ;;
    *)
        echo "usage: $0 {extract|update|compile|init <locale>}" >&2
        exit 2
        ;;
esac
