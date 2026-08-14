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
cache_dir="${2:-$repo_root/.cache/research/gigadevice/builder-firmware-v1}"

if [[ ! -f "$archive" ]]; then
    echo "Builder 归档不存在：$archive" >&2
    exit 1
fi

archive_sha256="$(shasum -a 256 "$archive" | awk '{print $1}')"
archive_files="$(mktemp "${TMPDIR:-/tmp}/builder-files.XXXXXX")"
trap 'rm -f "$archive_files"' EXIT
bsdtar -tf "$archive" >"$archive_files"

release="$({ sed -En 's#^([^/]+)/GD32EB/plugins/.*#\1#p' "$archive_files"; } | sort -u)"
resource_plugin="$({ sed -En 's#^([^/]+/GD32EB/plugins/com\.gigadevice\.resources_[^/]+)/(CodeGenerate|CodeTemplate)/.*#\1#p' "$archive_files"; } | sort -u)"
if [[ -z "$release" || "$release" == *$'\n'* || -z "$resource_plugin" || "$resource_plugin" == *$'\n'* ]]; then
    echo "无法唯一定位 Builder 发布目录或资源插件" >&2
    exit 1
fi

plugin_prefix="$release/GD32EB/plugins/"
output="$cache_dir/${release}-${archive_sha256:0:12}"
marker="$output/.complete"
tree_marker="$output/.tree-sha256"
tree_schema="$output/.tree-schema"

tree_sha256() {
    local root="$1"
    (
        cd "$root"
        find plugins resources -type f -exec shasum -a 256 {} + \
            | LC_ALL=C sort
    ) | shasum -a 256 | awk '{print $1}'
}

shape_valid() {
    [[ -f "$marker" && "$(<"$marker")" == "$archive_sha256" ]] || return 1
    [[ -d "$output/plugins" && -d "$output/resources/CodeGenerate" && -d "$output/resources/CodeTemplate" ]] || return 1
    [[ "$(find "$output/plugins" -mindepth 1 -maxdepth 1 -type d | awk 'END {print NR}')" -ge 36 ]] || return 1
}

verify_output() {
    shape_valid || return 1
    [[ -f "$tree_schema" && "$(<"$tree_schema")" == "2" ]] || return 1
    [[ -f "$tree_marker" && "$(<"$tree_marker")" == "$(tree_sha256 "$output")" ]] || return 1
}

print_summary() {
    local plugins files
    plugins="$(find "$output/plugins" -mindepth 1 -maxdepth 1 -type d | awk 'END {print NR}')"
    files="$(find "$output/plugins" "$output/resources" -type f | awk 'END {print NR}')"
    echo "已验证：${output}（固件插件=${plugins}，文件=${files}）"
}

mkdir -p "$cache_dir"
if verify_output; then
    print_summary
    exit 0
fi
if [[ -e "$output" ]]; then
    if shape_valid && [[ ! -e "$tree_schema" ]]; then
        tree_sha256 "$output" >"$tree_marker"
        printf '2\n' >"$tree_schema"
        verify_output
        print_summary
        exit 0
    fi
    echo "输出目录存在但校验失败，请人工检查后删除：$output" >&2
    exit 1
fi

work_dir="$(mktemp -d "$cache_dir/.builder-firmware.XXXXXX")"
trap 'rm -f "$archive_files"; rm -rf "$work_dir"' EXIT
mkdir -p "$work_dir/archive"

selectors=("$resource_plugin/CodeGenerate" "$resource_plugin/CodeTemplate")
while IFS= read -r plugin; do
    selectors+=("$plugin")
done < <(sed -En 's#^([^/]+/GD32EB/plugins/com\.gigadevice\.templatefwlib\.(arm|riscv)\.[^/]+)/.*#\1#p' "$archive_files" | sort -u)
if [[ "${#selectors[@]}" -lt 38 ]]; then
    echo "Builder 固件插件数量低于预期" >&2
    exit 1
fi

bsdtar -xf "$archive" -C "$work_dir/archive" "${selectors[@]}"
mkdir -p "$work_dir/result/plugins" "$work_dir/result/resources"
mv "$work_dir/archive/$resource_plugin/CodeGenerate" "$work_dir/result/resources/"
mv "$work_dir/archive/$resource_plugin/CodeTemplate" "$work_dir/result/resources/"
find "$work_dir/archive/$plugin_prefix" -mindepth 1 -maxdepth 1 -type d -name 'com.gigadevice.templatefwlib.*' \
    -exec mv {} "$work_dir/result/plugins/" \;
printf '%s\n' "$archive_sha256" >"$work_dir/result/.complete"
tree_sha256 "$work_dir/result" >"$work_dir/result/.tree-sha256"
printf '2\n' >"$work_dir/result/.tree-schema"

mv "$work_dir/result" "$output"
verify_output
print_summary
