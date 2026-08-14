#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "用法：$0 <cmsis-rust-target-db 仓库> [AddOn 解包目录] [输出目录]" >&2
}

if [[ $# -lt 1 || $# -gt 3 ]]; then
    usage
    exit 2
fi

for tool in cargo git python3; do
    command -v "$tool" >/dev/null || {
        echo "缺少必需工具：$tool" >&2
        exit 1
    }
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
target_db_repo="$(cd "$1" && pwd)"
pdsc_root="${2:-$repo_root/.cache/research/gigadevice/addon-packs-v1}"
out_dir="${3:-$repo_root/.cache/research/gigadevice/target-db-v1}"
target_dir="$repo_root/.cache/tools/cmsis-rust-target-db-target"

if [[ ! -f "$target_db_repo/Cargo.toml" ]]; then
    echo "无效的 cmsis-rust-target-db 仓库：$target_db_repo" >&2
    exit 1
fi
if [[ -n "$(git -C "$target_db_repo" status --porcelain)" ]]; then
    echo "cmsis-rust-target-db 工作区不干净，无法生成可复现锁文件" >&2
    exit 1
fi
if [[ ! -d "$pdsc_root" ]]; then
    echo "AddOn 解包目录不存在：$pdsc_root" >&2
    exit 1
fi

generator_revision="$(git -C "$target_db_repo" rev-parse HEAD)"
generator_repository="$(git -C "$target_db_repo" remote get-url origin)"
if [[ ! "$generator_revision" =~ ^[0-9a-f]{40}$ || -z "$generator_repository" ]]; then
    echo "无法锁定 cmsis-rust-target-db 来源" >&2
    exit 1
fi

mkdir -p "$target_dir" "$out_dir"
CARGO_TARGET_DIR="$target_dir" cargo run \
    --locked \
    --release \
    --manifest-path "$target_db_repo/Cargo.toml" \
    -- generate \
    --pdsc-root "$pdsc_root" \
    --out-dir "$out_dir"

python3 "$script_dir/filter_latest_target_db.py" \
    "$out_dir/devices.jsonl" \
    "$out_dir/latest" \
    --generator-repository "$generator_repository" \
    --generator-revision "$generator_revision" \
    --manifest "$repo_root/sources/gigadevice/target-db.lock.json"
