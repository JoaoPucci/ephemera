#!/usr/bin/env bash
# Cut an annotated, pushed, GitHub-released version tag in one sweep.
#
#   Usage: scripts/release.sh vX.Y.Z
#          scripts/release.sh X.Y.Z     (the `v` prefix is added for you)
#
# Flow:
#   1. Sanity-check the version format, the working tree, and the
#      absence of the tag on origin.
#   2. Open $EDITOR prefilled with a release-notes template. Save + quit
#      to continue.
#   3. Annotated tag + push + GitHub release -- all sharing the same
#      text, so the tag's message and the release body never drift.
#
# Requires: git, gh (authenticated), a remote named `origin`.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 vX.Y.Z" >&2
  exit 2
fi

version="$1"
[[ "$version" =~ ^v ]] || version="v${version}"

if [[ ! "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid version format: '$version' (expected vX.Y.Z)" >&2
  exit 2
fi

# Tag must not already exist, locally or on origin.
if git rev-parse --verify --quiet "refs/tags/$version" >/dev/null; then
  echo "Tag $version already exists locally." >&2
  exit 1
fi
if git ls-remote --tags origin | grep -qE "refs/tags/$version($|\^\{\})"; then
  echo "Tag $version already exists on origin." >&2
  exit 1
fi

# Soft-warn if we're cutting from a branch other than main.
branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" != "main" ]]; then
  echo "Warning: current branch is '$branch', not 'main'." >&2
  read -rp "Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || exit 1
fi

# Working tree must be clean so the tag points at a known commit.
if ! git diff-index --quiet HEAD --; then
  echo "Working tree has uncommitted changes. Commit or stash first." >&2
  exit 1
fi

# Seed the "Since vX.Y.Z:" line from the last tag, if any.
prev_tag="$(git describe --tags --abbrev=0 HEAD 2>/dev/null || echo 'none')"

tmpfile="$(mktemp -t "ephemera-release-XXXXXX.md")"
trap 'rm -f "$tmpfile"' EXIT

cat > "$tmpfile" <<EOF
$version -- <one-line summary>

Since $prev_tag:

<section>
  - ...
EOF

"${EDITOR:-vi}" "$tmpfile"

# Empty file -> abort. Template placeholder left in -> abort.
if ! [[ -s "$tmpfile" ]]; then
  echo "Release notes are empty -- aborting." >&2
  exit 1
fi
if grep -q '<one-line summary>' "$tmpfile"; then
  echo "Release notes still contain the template placeholder -- aborting." >&2
  exit 1
fi

echo
echo "Creating annotated tag $version..."
git tag -a "$version" -F "$tmpfile"

echo "Pushing tag to origin..."
git push origin "$version"

echo "Creating GitHub release..."
gh release create "$version" --title "$version" --notes-file "$tmpfile"
