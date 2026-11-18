#!/bin/sh
set -eu
app_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$app_directory/svn_git_migrator.py"
