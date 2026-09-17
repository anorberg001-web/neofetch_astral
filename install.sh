#!/usr/bin/env bash
set -euo pipefail

# Astral reads package.toml and performs this build itself. This script remains
# useful for manual installs and mirrors the declared Astral build command.
cargo build --release
