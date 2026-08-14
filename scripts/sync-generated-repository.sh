#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "用法：$0 <生成目录> <生成仓库>" >&2
    exit 2
fi

source_dir="$(cd "$1" && pwd)"
target_dir="$(cd "$2" && pwd)"

[[ -f "$source_dir/generation.json" && -f "$source_dir/mcu-metapac-generation.json" ]] || {
    echo "生成目录缺少发布标记：$source_dir" >&2
    exit 1
}
[[ -d "$target_dir/.git" && -d "$target_dir/.github" ]] || {
    echo "目标不是受保护的生成仓库：$target_dir" >&2
    exit 1
}

forbidden="$(find "$source_dir" -type f \( \
    -iname '*.7z' -o -iname '*.zip' -o -iname '*.rar' -o -iname '*.pack' -o \
    -iname '*.pdf' -o -iname '*.c' -o -iname '*.h' -o -iname '*.s' \
\) -print -quit)"
if [[ -n "$forbidden" ]]; then
    echo "发布目录包含原始厂商文件：$forbidden" >&2
    exit 1
fi

rsync -a --delete \
    --exclude '.git/' \
    --exclude '.github/' \
    --exclude '.gitignore' \
    --exclude 'Cargo.lock' \
    --exclude 'target/' \
    "$source_dir/" \
    "$target_dir/"

rm -f "$target_dir/Cargo.lock"
rm -rf "$target_dir/target"
