#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$script_dir/check-gigadevice-reproducibility.sh" "$@"
"$script_dir/check-rust.sh"
