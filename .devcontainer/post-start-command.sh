#!/bin/bash
set -ex

# For some reason the directory is not setup correctly and causes build of devcontainer to fail since
# it doesn't have access to the workspace directory. This can normally be done in post-start-command
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH='' cd -- "$script_dir/.." && pwd)"
git config --global --add safe.directory "$repo_root"
