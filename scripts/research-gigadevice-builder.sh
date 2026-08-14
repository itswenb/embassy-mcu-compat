#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "用法：$0 <GD32 Embedded Builder 归档> [输出缓存目录]" >&2
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
    usage
    exit 2
fi

for tool in bsdtar shasum find sort awk sed grep; do
    command -v "$tool" >/dev/null || {
        echo "缺少必需工具：$tool" >&2
        exit 1
    }
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
archive="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
cache_dir="${2:-$repo_root/.cache/research/gigadevice/builder-resources}"

if [[ ! -f "$archive" ]]; then
    echo "Builder 归档不存在：$archive" >&2
    exit 1
fi

archive_sha256="$(shasum -a 256 "$archive" | awk '{print $1}')"
plugin_prefix="$({
    bsdtar -tf "$archive" \
        | sed -En 's#^(.*/plugins/com\.gigadevice\.resources_[^/]+)/(AFIO|DataSheet)/.*#\1#p'
} | sort -u)"

if [[ -z "$plugin_prefix" || "$plugin_prefix" == *$'\n'* ]]; then
    echo "无法唯一定位 Builder 结构化资源插件" >&2
    exit 1
fi

release="${plugin_prefix%%/*}"
output="$cache_dir/${release}-${archive_sha256:0:12}"
marker="$output/.complete"
tree_marker="$output/.tree-sha256"

tree_sha256() {
    local root="$1"
    (
        cd "$root"
        find AFIO DataSheet -type f -name '*.xml' -print \
            | LC_ALL=C sort \
            | while IFS= read -r relative; do
                printf '%s\0' "$relative"
                shasum -a 256 "$relative" | awk '{print $1}'
            done
    ) | shasum -a 256 | awk '{print $1}'
}

verify_output() {
    [[ -f "$marker" ]] || return 1
    [[ "$(<"$marker")" == "$archive_sha256" ]] || return 1
    [[ -d "$output/AFIO" && -d "$output/DataSheet" ]] || return 1
    find "$output/AFIO" -type f -name '*.xml' -print -quit | grep -q . || return 1
    find "$output/DataSheet" -type f -name '*.xml' -print -quit | grep -q . || return 1
    [[ -f "$tree_marker" ]] || return 1
    [[ "$(<"$tree_marker")" == "$(tree_sha256 "$output")" ]] || return 1
}

print_summary() {
    local afio_count datasheet_count
    afio_count="$(find "$output/AFIO" -type f -name '*.xml' | awk 'END { print NR }')"
    datasheet_count="$(find "$output/DataSheet" -type f -name '*.xml' | awk 'END { print NR }')"
    echo "已验证：${output}（AFIO=${afio_count}，DataSheet=${datasheet_count}）"
}

mkdir -p "$cache_dir"
if verify_output; then
    print_summary
    exit 0
fi

if [[ -e "$output" ]]; then
    if [[ -f "$marker" && "$(<"$marker")" == "$archive_sha256" && ! -e "$tree_marker" ]]; then
        tree_sha256 "$output" >"$tree_marker"
        verify_output
        print_summary
        exit 0
    else
        echo "输出目录存在但校验失败，请人工检查后删除：$output" >&2
        exit 1
    fi
fi

work_dir="$(mktemp -d "$cache_dir/.builder-extract.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT
mkdir -p "$work_dir/archive"

bsdtar -xf "$archive" -C "$work_dir/archive" \
    "$plugin_prefix/AFIO" \
    "$plugin_prefix/DataSheet"

mv "$work_dir/archive/$plugin_prefix" "$work_dir/result"
find "$work_dir/result" -type f -name '*.xml' -print \
    | sed "s#^$work_dir/result/##" \
    | sort >"$work_dir/result/xml-files.txt"
printf '%s\n' "$archive_sha256" >"$work_dir/result/.complete"
tree_sha256 "$work_dir/result" >"$work_dir/result/.tree-sha256"

afio_count="$(find "$work_dir/result/AFIO" -type f -name '*.xml' | awk 'END { print NR }')"
datasheet_count="$(find "$work_dir/result/DataSheet" -type f -name '*.xml' | awk 'END { print NR }')"
if [[ "$afio_count" -eq 0 || "$datasheet_count" -eq 0 ]]; then
    echo "Builder 归档缺少预期 XML 数据" >&2
    exit 1
fi

mv "$work_dir/result" "$output"
verify_output
print_summary
