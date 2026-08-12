# embassy-mcu-compat

本仓库在不修改 `embassy-stm32`、`stm32-data` 和官方 `stm32-data-generated` 的前提下，生成可被 Cargo patch 替换的 `stm32-metapac`。应用仍启用一个官方 STM32 alias feature；构建脚本再通过 `EMBASSY_MCU_COMPAT_CHIP` 把 PAC、metadata 和链接脚本切换到经过审计的真实 MCU 目录。

当前来源闭包覆盖 12 个 GigaDevice DFP、388 个规范型号。`GD32F103C8 -> STM32F103C8` 仅用于端到端测试，当前没有达到 release 证据门的国产芯片；不要把示例解释为量产支持。

## 数据边界

- `cmsis-rust-target-db` 提供厂商、型号、Pack、CPU、FPU、字节序和 Rust target 事实。
- CMSIS-Pack 的 PDSC、SVD、头文件和锁定归档提供可定位的厂商证据。
- 固定 revision 的 STM32 Chip JSON 是显式 alias 的生成基线，RFC 7396 patch 只记录真实差异。
- 固定 revision 的 `stm32-metapac-gen` 生成真实芯片私有目录；共享 peripheral/register 模块必须与官方生成仓库逐字节一致。

`cmsis-rust-target-db` 不提供 Embassy 所需的 pinmux、DMA、RCC、Flash 算法或完整寄存器语义。这些内容不能从 CPU 事实推导，缺少证据时型号只能保持 `unmapped` 或 `blocked`。

## 本地运行

安装 `rust-toolchain.toml` 锁定的 Rust 工具链和 `sources.lock.toml` 锁定的 CMSIS-Toolbox。只有 `sources update` 联网；下载内容保存在 `.cache/sources`，不会进入 Git。

```bash
# 1. 还原并验证锁定的目标数据库、Pack Index 和全部 Pack。
cargo run --release -- sources update --cache-dir .cache/sources

# 2. 离线重算审计报告，任何字节漂移都失败。
cargo run --release -- audit --frozen --cache-dir .cache/sources

# 3. 从干净、固定 revision 的官方生成仓库产生兼容 metapac。
cargo run --release -- generate \
  --official-generated ../stm32-data-generated \
  --output ../embassy-mcu-compat-generated \
  --cache-dir .cache/sources
```

`generate` 要求输出目录不存在或为空，验证完成前只写同一父目录下的临时目录。`--include-test` 才会加入 test 映射，定时生成仓库当前使用该参数保留端到端验证芯片。

## Cargo patch 用法

应用依旧声明官方 alias：

```toml
[dependencies]
embassy-stm32 = { version = "...", features = ["stm32f103c8"] }

[patch."https://github.com/embassy-rs/stm32-data-generated"]
stm32-metapac = { git = "https://github.com/itswenb/embassy-mcu-compat-generated", rev = "<固定提交>" }
```

如果 `embassy-stm32` 来自 crates.io，应把 patch 表改成与其实际依赖源匹配的 `[patch.crates-io]`。真实型号不成为 Cargo feature，也不会进入 `ALL_CHIPS`。

测试 GD 路径还需在项目的 `.cargo/config.toml` 中显式选择真实型号：

```toml
[env]
EMBASSY_MCU_COMPAT_CHIP = { value = "gd32f103c8", force = true }
```

未设置变量时完全保留原生 STM32 路径。未知型号、错误 alias、零个或多个 STM32 feature 都会在构建期失败。固定的 Embassy commit 要求 Rust 1.97+；可分别进入 `examples/stm32f103c8-native` 与 `examples/gd32f103c8` 后运行 `cargo +1.97.0 check --target thumbv7m-none-eabi`，查看两条最小 `no_std` 编译路径。

## Release 证据门

release 映射必须同时通过 CPU、memory、interrupts、registers、RCC、Flash、pins、DMA、alias cfg、license 和硬件验证。生成器只选择 `scope = "release"` 且没有 blocker 的映射；test 映射永远需要显式 `--include-test`。

每次升级必须把 Pack Index、`cmsis-rust-target-db`、Embassy、`stm32-data`、`stm32-data-generated`、`chiptool`、CMSIS-Toolbox 和 Pack 哈希作为一个整体复核。滚动目标数据库时显式传入 commit：

```bash
cargo run --release -- sources update \
  --target-db-revision <40 位 commit> \
  --cache-dir .cache/sources
cargo run --release -- audit --update-derived --cache-dir .cache/sources
cargo run --release -- audit --frozen --cache-dir .cache/sources
```

`--update-derived` 只用于来源维护流程更新确定性的 `reports/inventory.json`；普通 CI 始终使用冻结比较。GitHub Actions 每周三依次同步 `cmsis-rust-target-db`、本来源仓库和生成仓库，任一 revision、哈希、审计、共享模块或目标编译不一致都会停止提交。
