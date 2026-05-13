#!/bin/bash
# Stage repo content into _site_build/ for `mkdocs serve` (or build).
#
# Mirror of the "Stage content for build" step in
# .github/workflows/sync.yml. mkdocs.yml's docs_dir points at
# _site_build/ rather than the repo root because MkDocs forbids
# docs_dir from being the same dir as the config file. CI runs this
# automatically; locally, run it any time .pages, INDEX.md, articles/,
# or the static asset dirs change.
#
# Usage: ./stage-local.sh && .venv/bin/mkdocs serve

set -euo pipefail
cd "$(dirname "$0")"

rm -rf _site_build
mkdir -p _site_build
cp -R articles/. _site_build/
cp -R stylesheets _site_build/
cp -R javascripts _site_build/
sed 's|articles/||g' .pages > _site_build/.pages
sed 's|articles/||g' INDEX.md > _site_build/index.md
cp index.json _site_build/

echo "Staged content into _site_build/"
