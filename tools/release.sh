#!/bin/sh
# Publish a GitHub release from this machine (no CI needed): packages the
# tagged tree as xlinux.tar.gz and uploads it with install.sh through `gh`.
#
#   tools/release.sh            # version from xlinux/__init__.py
#   tools/release.sh --dry-run  # only build dist/ to inspect it
set -eu

cd "$(dirname "$0")/.."
version=$(python3 -c 'import xlinux; print(xlinux.__version__)')
tag="v$version"
dry_run=false
[ "${1:-}" = --dry-run ] && dry_run=true

[ -z "$(git status --porcelain)" ] || { echo "error: the working tree has uncommitted changes" >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    [ "$(git rev-parse "$tag^{commit}")" = "$(git rev-parse HEAD)" ] ||
        { echo "error: $tag already exists on another commit; bump __version__" >&2; exit 1; }
elif ! $dry_run; then
    git tag -a "$tag" -m "xlinux $version"
fi

rm -rf dist && mkdir dist
# Tracked files only (see .gitattributes for export-ignore), symlinks kept.
git archive --format=tar.gz --prefix=xlinux/ -o dist/xlinux.tar.gz HEAD
cp install.sh dist/install.sh
(cd dist && sha256sum xlinux.tar.gz install.sh > SHA256SUMS)
ls -l dist

if $dry_run; then
    echo "dry run: nothing published" >&2
    exit 0
fi
git push origin "$tag"
gh release create "$tag" dist/xlinux.tar.gz dist/install.sh dist/SHA256SUMS \
    --title "xlinux $version" --generate-notes --latest
