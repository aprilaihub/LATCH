#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

usage() {
  cat <<'USAGE'
Usage: scripts/export_git_archive.sh [--output <path>]

Create a review-ready zip archive from committed HEAD using git archive.
Only Git-tracked files from this repository are included; submodules are
preserved as pointers rather than recursively copied.

Options:
  --output <path>  Write archive to a custom .zip path.
  -h, --help       Show this help message.
USAGE
}

output=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --output)
      output="${2:?Error: --output requires a path}"
      shift 2
      ;;
    *)
      echo "Error: unknown argument '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -d .git ]]; then
  echo "Error: this script must be run from a repository checkout with .git present." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git is not installed or not available on PATH." >&2
  exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached")"
commit="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
repo_name="$(basename "$REPO_ROOT")"
safe_branch="$(printf '%s' "$branch" | sed 's#[^[:alnum:]._-]#-#g; s#--*#-#g; s#^-##; s#-$##')"
if [[ -z "$safe_branch" ]]; then
  safe_branch="detached"
fi

if [[ -z "$output" ]]; then
  timestamp="$(date +%Y%m%d_%H%M%S)"
  output="git_archives/${repo_name}_${safe_branch}_${commit}_${timestamp}.zip"
fi

case "$output" in
  *.zip) ;;
  *)
    echo "Error: output path must end with .zip: $output" >&2
    exit 2
    ;;
esac

if [[ -e "$output" ]]; then
  echo "Error: output already exists: $output" >&2
  exit 1
fi

dirty_status="$(git status --porcelain=v1)"
if [[ -n "$dirty_status" ]]; then
  echo "Warning: working tree is dirty; archive will still be created from committed HEAD." >&2
fi

mkdir -p "$(dirname "$output")"
prefix="${repo_name}-${commit}"
git archive --format=zip --prefix="${prefix}/" --output "$output" HEAD

echo "Packed committed HEAD:"
echo "  branch: $branch"
echo "  commit: $commit"
echo "  output: $output"
echo "Unzip hint:"
echo "  unzip $output"
