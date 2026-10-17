#!/usr/bin/env bash
# Build fictional author history in a separate temporary copy, never in this checkout.
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:-$(mktemp -d "${TMPDIR:-/tmp}/ledgerlane-history.XXXXXX")}"
mkdir -p "$target"
target="$(cd "$target" && pwd)"
case "$target/" in
  "$source_dir/"*) echo "Target must be outside demo_project" >&2; exit 2 ;;
esac
if [[ -e "$target/.git" ]] || [[ -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "Target must be an empty directory: $target" >&2
  exit 2
fi
cp -R "$source_dir/." "$target/"
cd "$target"
git init -q
git config core.hooksPath /dev/null
git config commit.gpgSign false
git config user.name "Ledgerlane Demo"
git config user.email "demo@ledgerlane.example"

commit_as() {
  local name="$1" email="$2" message="$3"
  GIT_AUTHOR_NAME="$name" GIT_AUTHOR_EMAIL="$email" \
  GIT_COMMITTER_NAME="$name" GIT_COMMITTER_EMAIL="$email" \
    git commit -q -m "$message"
}

git add auth db
commit_as "Avery Chen" "avery@ledgerlane.example" "Add JWT auth and Postgres records"
git add billing notifications
commit_as "Morgan Patel" "morgan@ledgerlane.example" "Add invoice and email flow"
git add web tests README.md .gitignore fake_history.sh
commit_as "Riley Okafor" "riley@ledgerlane.example" "Add web routes and auth checks"

echo "$target"
