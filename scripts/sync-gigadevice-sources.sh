#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo "用法：$0 [cmsis-rust-target-db 仓库]" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

if [[ $# -eq 1 ]]; then
    target_db_repo="$(cd "$1" && pwd)"
else
    target_db_repo="$repo_root/.cache/research/repos/cmsis-rust-target-db"
fi

cd "$repo_root"

update_plan=".cache/research/gigadevice/update-plan.json"
python3 scripts/plan_gigadevice_update.py --output "$update_plan"
action="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["action"])' "$update_plan")"
if [[ "$action" == "noop" ]]; then
    echo "官网来源与派生流水线均无变化，跳过下载、生成和编译。"
    exit 0
fi

if [[ "$action" == "materialize" ]]; then
    for variable in \
        GIGADEVICE_ACCEPT_SLA_GD0001 \
        GIGADEVICE_ACCEPT_SLA_GD0003 \
        GIGADEVICE_ACCEPT_SLA_GD0006; do
        if [[ "${!variable:-}" != "1" ]]; then
            echo "物化来源前必须显式设置 ${variable}=1" >&2
            exit 1
        fi
    done

    if [[ $# -eq 1 ]]; then
        python3 scripts/sync_upstream_research.py
    else
        python3 scripts/sync_upstream_research.py --include-target-db
    fi
    python3 scripts/gigadevice_sources.py \
        --download \
        --manifest sources/gigadevice/firmware.lock.json \
        --extract-dir .cache/research/gigadevice/firmware-sources-v1

    python3 scripts/gigadevice_addons.py \
        --download \
        --extract-dir .cache/research/gigadevice/addon-packs-v1

    python3 scripts/gigadevice_builder.py \
        --download \
        --extract

    python3 scripts/gigadevice_official_tool.py --download
    python3 scripts/gigadevice_iar.py --download
    python3 scripts/gigadevice_catalog.py --download
    python3 scripts/gigadevice_products.py
    python3 scripts/gigadevice_manuals.py --download
    python3 scripts/gigadevice_manuals.py --kind datasheet --download
fi

if [[ ! -d "$target_db_repo" ]]; then
    echo "派生所需目标数据库缓存不存在：$target_db_repo" >&2
    exit 1
fi
target_db_repo="$(cd "$target_db_repo" && pwd)"

python3 scripts/scan_gigadevice_programmer.py
python3 scripts/index_gigadevice_programmer.py

python3 scripts/index_gigadevice_builder_firmware.py
python3 scripts/adapt_gigadevice_builder_firmware.py
builder_sha="$(python3 -c 'import json; print(json.load(open("sources/gigadevice/builder.lock.json", encoding="utf-8"))["builder"]["sha256"][:12])')"
builder_adapter=".cache/research/gigadevice/builder-firmware-adapter-v1/$builder_sha"

python3 scripts/extract_gigadevice_manual_text.py
python3 scripts/extract_gigadevice_manual_text.py --kind datasheet
python3 scripts/extract_gigadevice_manual_dma.py
python3 scripts/extract_gigadevice_datasheet_pins.py
python3 scripts/index_gigadevice_riscv.py

scripts/generate-gigadevice-target-db.sh "$target_db_repo"
python3 scripts/normalize_gigadevice_models.py
iar_sha="$(python3 -c 'import json; print(json.load(open("sources/gigadevice/iar.lock.json", encoding="utf-8"))["iar"]["sha256"][:12])')"
iar_root=".cache/research/gigadevice/iar-device-support-v1/$iar_sha"
python3 scripts/gigadevice_iar.py --locked
python3 scripts/analyze_gigadevice_builder_models.py
python3 scripts/normalize_gigadevice_builder_pins.py
python3 scripts/normalize_gigadevice_pins.py
python3 scripts/index_gigadevice_pack_resources.py
python3 scripts/normalize_gigadevice_memory.py
python3 scripts/audit_gigadevice_svds.py
python3 scripts/compile_gigadevice_pacs.py
python3 scripts/audit_gigadevice_svds.py \
    --resources reports/gigadevice-iar-a7.json \
    --pdsc-root "$iar_root" \
    --output reports/gigadevice-iar-svd-audit.json \
    --minimum-svd-files 3
python3 scripts/compile_gigadevice_pacs.py \
    --audit reports/gigadevice-iar-svd-audit.json \
    --output reports/gigadevice-iar-pac-compile.json \
    --minimum-pac-outputs 3
python3 scripts/index_gigadevice_firmware_headers.py
python3 scripts/extract_gigadevice_firmware_registers.py
python3 scripts/build_gigadevice_firmware_variants.py --minimum-devices 680
python3 scripts/extract_gigadevice_firmware_registers.py \
    --lock "$builder_adapter/lock.json" \
    --headers reports/gigadevice-builder-headers.json \
    --root "$builder_adapter/libraries" \
    --output reports/gigadevice-builder-registers.json \
    --minimum-firmware-libraries 36
python3 scripts/build_gigadevice_firmware_variants.py \
    --registers reports/gigadevice-builder-registers.json \
    --lock "$builder_adapter/lock.json" \
    --known-source-issues sources/gigadevice/builder-firmware-register-conflicts.json \
    --root "$builder_adapter/libraries" \
    --cache-dir .cache/research/gigadevice/builder-firmware-cpp-v1 \
    --output reports/gigadevice-builder-variants.json \
    --minimum-devices 680
python3 scripts/audit_gigadevice_model_universe.py --minimum-devices 680
python3 scripts/merge_gigadevice_firmware_variants.py \
    --minimum-devices 680 \
    --maximum-missing-devices 48
python3 scripts/normalize_gigadevice_embassy_names.py
python3 scripts/normalize_gigadevice_rcu.py \
    --variants reports/gigadevice-merged-firmware-variants.json
python3 scripts/normalize_gigadevice_dma.py \
    --variants reports/gigadevice-merged-firmware-variants.json
python3 scripts/generate_gigadevice_firmware_pacs.py \
    --variants reports/gigadevice-merged-firmware-variants.json \
    --minimum-devices 632
python3 scripts/analyze_gigadevice_stm32_register_compat.py
python3 scripts/generate_gigadevice_stm32_data.py --minimum-devices 632
cargo run --quiet --bin m32-metapac-gen -- \
    --data-dir .cache/generated/gigadevice-stm32-data-v1 \
    --output .cache/generated/gigadevice-metapac-v1 \
    --report reports/gigadevice-metapac.json
python3 scripts/augment_gigadevice_iar_metapac.py \
    --source-root "$iar_root" \
    --replace
python3 scripts/check_gigadevice_metapac.py \
    --metapac-dir .cache/generated/gigadevice-metapac-complete-v1 \
    --minimum-devices 680 \
    --offline
python3 scripts/compare_gigadevice_svd_headers.py
python3 scripts/build_gigadevice_mcu_data.py \
    --variants reports/gigadevice-merged-firmware-variants.json
python3 scripts/analyze_gigadevice_coverage.py
python3 scripts/analyze_embassy_boundary.py
python3 -m unittest discover -s scripts/tests -p 'test_*.py'

official_generated=".cache/research/repos/stm32-data-generated"
if [[ ! -d "$official_generated/.git" ]]; then
    echo "缺少锁定的官方 stm32-data-generated：$official_generated" >&2
    exit 1
fi
mkdir -p .cache/generated
patch_workspace="$(mktemp -d "$repo_root/.cache/generated/.stm32-patch.XXXXXX")"
trap 'rm -rf "$patch_workspace"' EXIT
cargo run --quiet --bin mcu-compat-gen -- generate \
    --official-generated "$official_generated" \
    --output "$patch_workspace/output"
mkdir -p .cache/generated/stm32-metapac-patch-v1
rsync -a --delete "$patch_workspace/output/" .cache/generated/stm32-metapac-patch-v1/

python3 scripts/publish_mcu_metapac.py \
    --patch .cache/generated/stm32-metapac-patch-v1 \
    --native .cache/generated/gigadevice-metapac-complete-v1 \
    --output .cache/generated/mcu-metapac-publication-v1 \
    --replace
mkdir -p .cache/generated/mcu-metapac-publication-v1/release
cp reports/gigadevice-metapac-compile.json \
    .cache/generated/mcu-metapac-publication-v1/release/
cp reports/gigadevice-model-universe.json \
    .cache/generated/mcu-metapac-publication-v1/release/
cp reports/gigadevice-iar-pac-compile.json \
    .cache/generated/mcu-metapac-publication-v1/release/
python3 scripts/plan_gigadevice_update.py \
    --output "$update_plan" \
    --success-marker .cache/generated/mcu-metapac-publication-v1/gigadevice-sync-success.json \
    --mark-success
python3 scripts/gigadevice_inventory.py
cp reports/gigadevice-inventory.json \
    .cache/generated/mcu-metapac-publication-v1/gigadevice-inventory.json

generated_repo="${GENERATED_REPOSITORY_DIR:-$repo_root/../embassy-mcu-compat-generated}"
if [[ -d "$generated_repo/.git" ]]; then
    scripts/sync-generated-repository.sh \
        .cache/generated/mcu-metapac-publication-v1 \
        "$generated_repo"
else
    echo "未配置 generated 仓库工作树，仅保留本地发布目录：$generated_repo"
fi
cp .cache/generated/mcu-metapac-publication-v1/gigadevice-sync-success.json \
    reports/gigadevice-sync-success.json
