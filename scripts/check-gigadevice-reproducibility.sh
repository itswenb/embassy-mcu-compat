#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo "用法：$0 [cmsis-rust-target-db 仓库]" >&2
    exit 2
fi

for tool in awk shasum; do
    command -v "$tool" >/dev/null || {
        echo "缺少必需工具：$tool" >&2
        exit 1
    }
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

outputs=(
    sources/gigadevice/firmware.lock.json
    sources/gigadevice/addons.lock.json
    sources/gigadevice/builder.lock.json
    sources/gigadevice/programmer.lock.json
    sources/gigadevice/catalog.lock.json
    sources/gigadevice/products.lock.json
    sources/gigadevice/manuals.lock.json
    sources/gigadevice/datasheets.lock.json
    sources/gigadevice/target-db.lock.json
    sources/gigadevice/iar.lock.json
    reports/gigadevice-catalog.json
    reports/gigadevice-products.json
    reports/gigadevice-manuals.json
    reports/gigadevice-datasheets.json
    reports/gigadevice-datasheet-pins.json
    reports/gigadevice-manual-dma.json
    reports/gigadevice-models.json
    reports/gigadevice-model-universe.json
    reports/gigadevice-iar-a7.json
    reports/gigadevice-iar-svd-audit.json
    reports/gigadevice-iar-pac-compile.json
    reports/gigadevice-programmer.json
    reports/gigadevice-programmer-data.json
    reports/gigadevice-builder-firmware.json
    reports/gigadevice-builder-headers.json
    reports/gigadevice-builder-models.json
    reports/gigadevice-builder-pins.json
    reports/gigadevice-pins.json
    reports/gigadevice-pack-resources.json
    reports/gigadevice-memory.json
    reports/gigadevice-svd-audit.json
    reports/gigadevice-pac-compile.json
    reports/gigadevice-firmware-headers.json
    reports/gigadevice-firmware-registers.json
    reports/gigadevice-firmware-variants.json
    reports/gigadevice-builder-registers.json
    reports/gigadevice-builder-variants.json
    reports/gigadevice-merged-firmware-variants.json
    reports/gigadevice-embassy-names.json
    reports/gigadevice-rcu.json
    reports/gigadevice-riscv.json
    reports/gigadevice-dma.json
    reports/gigadevice-firmware-pac-compile.json
    reports/gigadevice-stm32-register-compat.json
    reports/gigadevice-stm32-data.json
    reports/gigadevice-metapac.json
    reports/gigadevice-metapac-compile.json
    reports/gigadevice-complete-metapac.json
    reports/gigadevice-svd-header-comparison.json
    reports/gigadevice-mcu-data.json
    reports/gigadevice-source-coverage.json
    reports/embassy-stm32-boundary.json
)

digest() {
    local path
    for path in "${outputs[@]}"; do
        [[ -f "$path" ]] || {
            echo "缺少派生文件：$path；请先运行同步脚本" >&2
            return 1
        }
    done
    shasum -a 256 "${outputs[@]}" | shasum -a 256 | awk '{print $1}'
}

before="$(digest)"
"$script_dir/sync-gigadevice-sources.sh" "$@"
after="$(digest)"

if [[ "$before" != "$after" ]]; then
    echo "重复同步产生不同结果：before=$before after=$after" >&2
    exit 1
fi

echo "重复同步结果一致：$after"
