# `embassy-mcu-compat` 实施计划

> **执行要求：** 必须使用子技能 `superpowers:executing-plans` 逐任务实施本计划。

**目标：** 在不修改 `embassy-stm32`、`stm32-data` 和官方 `stm32-data-generated` 的前提下，完成厂商无关的 MCU 审计与 `stm32-metapac` 兼容生成链；首版全量覆盖当前 12 个 GigaDevice DFP 的 388 个型号，并用 test-only 的 `GD32F103C8 -> STM32F103C8` 验证 Cargo patch 端到端路径。

**架构：** `cmsis-rust-target-db` 的固定 JSONL 快照负责器件发现与 CPU/Rust target 归一化；兼容文件只保存显式 STM32 alias、证据和 RFC 7396 patch；生成器以官方 `stm32-data-generated` 的 `stm32-metapac` 与 `data/` 为基线，在临时目录调用固定 revision 的 `stm32-metapac-gen`，最终只增加真实芯片私有目录、兼容选择表和自定义 `build.rs`。真实型号由 `EMBASSY_MCU_COMPAT_CHIP` 选择，未设置时保持原生 STM32 行为。

**技术栈：** Rust 2024、`clap`、`serde`/`serde_json`/`toml`、`roxmltree`、`sha2`、`tempfile`、固定 git revision 的 `stm32-data-serde` 与 `stm32-metapac-gen`、CMSIS-Toolbox 2.14.1（`cpackget` 2.2.1）。

---

## 不变量与验收基线

- 不向 `../stm32-data` 写入任何文件；其现有未跟踪 `docs/` 也不得纳入本项目提交。
- 不修改或 fork `cmsis-rust-target-db`；固定消费 revision `947e8a96d462801e827ed76408c8d8457326f6a1`。
- `embassy` 固定为 `98d847be57f3ea022ce05fe9b95ab3639a1e0a93`。
- `stm32-data` 固定为 `87c539515764df442bc50b6235bad891950ba3c4`。
- `stm32-data-generated` 固定为 `12ec4cd38c7825c1ff8592de1bdefaae445bb3a6`。
- `chiptool` 由上游锁定为 `be1bff3e9e1b27b090e69bd9ac753c66fdcce678`。
- GigaDevice 首版必须得到 12 个 Pack、388 个审计型号、0 个重复型号和 0 个无法解析 Rust target 的型号。
- test-only 映射只能得到 `blocked`，绝不能进入默认发布输出。
- 任何失败均只污染临时目录；已有输出目录非空时拒绝执行。

## 预期文件树

```text
embassy-mcu-compat/
├── Cargo.toml
├── Cargo.lock
├── rust-toolchain.toml
├── .gitignore
├── sources.lock.toml
├── compat/
│   └── gigadevice/gd32f103c8.json
├── evidence/
│   └── README.md
├── reports/
│   └── inventory.json
├── examples/
│   ├── gd32f103c8/
│   └── stm32f103c8-native/
├── src/
│   ├── main.rs
│   ├── cli.rs
│   ├── hash.rs
│   ├── lock.rs
│   ├── mapping.rs
│   ├── merge_patch.rs
│   ├── pdsc.rs
│   ├── report.rs
│   ├── sources.rs
│   ├── target_db.rs
│   └── generate.rs
└── tests/
    ├── cli.rs
    ├── fixtures/
    ├── inventory.rs
    ├── mapping.rs
    └── generation.rs
```

不创建 workspace、插件接口、内部数据库 crate 或厂商专用 Rust 模块。

---

### 任务 1：搭建单包 CLI 与固定工具链

**文件：**

- 新建：`Cargo.toml`
- 新建：`rust-toolchain.toml`
- 新建：`.gitignore`
- 新建：`src/main.rs`
- 新建：`src/cli.rs`
- 新建：`tests/cli.rs`

**步骤 1：先写失败的 CLI 测试**

测试通过 `env!("CARGO_BIN_EXE_mcu-compat-gen")` 调用二进制，并断言帮助中只出现三个顶层动作：`sources`、`audit`、`generate`；`sources` 下只出现 `update`。

```rust
#[test]
fn exposes_only_the_supported_top_level_commands() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_mcu-compat-gen"))
        .arg("--help")
        .output()
        .unwrap();
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(output.status.success());
    for command in ["sources", "audit", "generate"] {
        assert!(stdout.contains(command));
    }
}
```

运行：

```bash
rtk cargo test --test cli
```

预期：失败，因为二进制尚不存在。

**步骤 2：创建最小 Cargo 包**

`Cargo.toml` 只加入实际使用的依赖：

```toml
[package]
name = "mcu-compat-gen"
version = "0.1.0"
edition = "2024"
license = "MIT OR Apache-2.0"

[dependencies]
anyhow = "1"
clap = { version = "4", features = ["derive"] }
roxmltree = "0.20"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
sha2 = "0.10"
tempfile = "3"
toml = "0.9"
walkdir = "2"
stm32-data-serde = { git = "https://github.com/embassy-rs/stm32-data", rev = "87c539515764df442bc50b6235bad891950ba3c4" }
stm32-metapac-gen = { git = "https://github.com/embassy-rs/stm32-data", rev = "87c539515764df442bc50b6235bad891950ba3c4" }
```

`rust-toolchain.toml` 与固定 `stm32-data` revision 一致，使用 `nightly-2025-12-11`，包含 `rustfmt` 及所需 Thumb targets。

**步骤 3：实现参数解析，不实现业务占位代码**

`src/cli.rs` 定义：

```text
mcu-compat-gen sources update --cache-dir <DIR> [--lock <FILE>]
mcu-compat-gen audit --frozen --cache-dir <DIR> [--lock <FILE>] [--compat-dir <DIR>] [--output <FILE>]
mcu-compat-gen generate --official-generated <DIR> --output <DIR> [--include-test]
```

未实现的分支返回带中文上下文的错误，不使用 `todo!()`。

**步骤 4：验证并提交**

```bash
rtk cargo fmt --all -- --check
rtk cargo test --test cli
rtk cargo check
rtk git add Cargo.toml Cargo.lock rust-toolchain.toml .gitignore src tests/cli.rs
rtk git commit -m "构建：初始化 MCU 兼容生成器"
```

预期：全部通过；提交只包含新仓库文件。

---

### 任务 2：接入 `cmsis-rust-target-db` 并规范化审计型号

**文件：**

- 新建：`src/target_db.rs`
- 新建：`tests/fixtures/target-db/metadata.json`
- 新建：`tests/fixtures/target-db/devices.jsonl`
- 新建：`tests/inventory.rs`
- 修改：`src/main.rs`

**步骤 1：写四个失败测试**

fixture 最少包含：一个无 variant 的 device、一个含 variant 的父 device、两个 variant、一个双处理器 device、一个同名冲突记录。分别断言：

1. 有 variant 时不保留父 device；
2. 无 variant 的 device 保留；
3. 同一器件的处理器记录合并并按 `processor` 排序；
4. 同一规范名来自两个选中 Pack 时返回错误，不猜测优先级；
5. `metadata.source_index_sha256` 与请求索引不一致时失败。

运行：

```bash
rtk cargo test --test inventory target_db
```

预期：失败，模块不存在。

**步骤 2：实现流式 JSONL 读取与最小模型**

`src/target_db.rs` 只定义消费 schema 1 所需字段：`device`、`device_kind`、`parent_device`、处理器属性、Rust target 和 Pack 溯源字段；未知 JSON 字段由 Serde 忽略。

规范化键为 `(source_pack_vendor, source_pack_name, source_pack_version, canonical_chip)`。`canonical_chip` 仅接受 ASCII 字母、数字和 `-`，输出小写。变体去父项和多处理器合并在一次确定性 `BTreeMap` 归并中完成。

**步骤 3：实现通用来源筛选**

首版模式只需支持一个 `*` 通配符，足以表达 `*_DFP`；不引入正则或 glob 依赖。匹配函数独立测试开头、结尾和中间通配。

**步骤 4：验证并提交**

```bash
rtk cargo fmt --all -- --check
rtk cargo test --test inventory
rtk cargo clippy --all-targets -- -D warnings
rtk git add src/target_db.rs src/main.rs tests/fixtures/target-db tests/inventory.rs
rtk git commit -m "功能：读取并规范化 CMSIS 目标数据库"
```

---

### 任务 3：实现来源锁、哈希和安全的 `sources update`

**文件：**

- 新建：`src/hash.rs`
- 新建：`src/lock.rs`
- 新建：`src/sources.rs`
- 新建：`tests/fixtures/source-lock.toml`
- 新建：`tests/lock.rs`
- 新建：`sources.lock.toml`
- 修改：`src/cli.rs`
- 修改：`src/main.rs`

**步骤 1：先写锁文件 round-trip 与漂移测试**

断言：

- TOML round-trip 不丢字段，重新编码字节一致；
- Pack、器件和处理器均稳定排序；
- 文件 SHA-256 不匹配时错误中包含相对路径、期望值和实际值；
- 锁文件中的 target DB schema 不是 `1` 时失败；
- 目标数据库索引 SHA 与 `index.pidx` 不同就失败。

运行：

```bash
rtk cargo test --test lock
```

预期：失败。

**步骤 2：实现锁模型**

锁文件包含：

- Pack Index URL、时间戳、SHA-256；
- target DB URL、revision、schema、两个数据文件哈希；
- 来源选择器列表；
- Pack ID、版本、归档哈希、安装树哈希、PDSC 相对路径；
- 规范化审计器件与处理器事实；
- `embassy`、`stm32-data`、`stm32-data-generated`、`chiptool` revision 和 `stm32-metapac` 版本。

哈希实现只使用 `sha2`；目录哈希按规范化相对路径排序，将路径长度、路径字节、文件长度和文件内容依次喂给 SHA-256，避免简单拼接歧义。

**步骤 3：把联网行为限制在 `sources update`**

`src/sources.rs` 通过 `std::process::Command`：

1. 将 target DB clone 到缓存并 checkout 固定 revision；
2. 校验 `data/devices.jsonl`、`data/metadata.json` 和索引哈希；
3. 以 `CMSIS_PACK_ROOT=<cache>/cmsis` 运行 `cpackget init -C 1 <index-url> --all-pdsc-files`；
4. 对筛选出的 Pack 运行 `cpackget add <vendor>::<name>@<version>`；
5. 定位 `.Download` 中的 `.pack` 与安装目录，计算哈希；
6. 先写临时锁文件，完整反序列化并自校验后再原子 rename。

外部命令缺失、退出非零或输出目录结构不符合预期时返回中文上下文；不得自动安装工具、接受 EULA、删除缓存或覆盖非锁文件。

**步骤 4：填入首版固定来源登记**

初始 `sources.lock.toml` 登记 `vendor = "GigaDevice"`、`pack_pattern = "*_DFP"` 和全部固定 revision。下载后生成的 Pack/器件区段不得手写。

**步骤 5：验证并提交**

```bash
rtk cargo fmt --all -- --check
rtk cargo test --test lock
rtk cargo test --test inventory
rtk git add src/hash.rs src/lock.rs src/sources.rs src/cli.rs src/main.rs tests/lock.rs tests/fixtures/source-lock.toml sources.lock.toml
rtk git commit -m "功能：锁定并校验 CMSIS 数据来源"
```

---

### 任务 4：实现兼容映射 schema、证据门和 RFC 7396 patch

**文件：**

- 新建：`src/mapping.rs`
- 新建：`src/merge_patch.rs`
- 新建：`tests/mapping.rs`
- 新建：`compat/gigadevice/gd32f103c8.json`
- 新建：`evidence/README.md`

**步骤 1：写失败的 Merge Patch 测试**

覆盖对象递归合并、标量替换、数组整体替换和 `null` 删除；测试必须使用 RFC 7396 的标准行为，不添加私有语义。

**步骤 2：写映射约束测试**

断言：

- `chip`、文件名和清单型号一致；
- `alias` 必须是 `stm32...` 规范小写名称；
- `scope` 只接受 `test` 或 `release`；
- release 映射缺少任一必需证据类别时失败；
- test 映射永远不能得到 `ready`；
- 映射引用不存在的 Pack/device 时失败；
- 映射声明的 `rust_target` 必须等于 target DB 事实。

必需证据类别固定为设计规格中的十类，不设计插件：`cpu`、`memory`、`interrupts`、`registers`、`rcc`、`flash`、`pins`、`dma`、`alias_cfg`、`license`，另有 `hardware` 作为 release 硬件门。

**步骤 3：实现最小证据引用**

每条证据只有 `path`、`sha256`、`locator`、`result`。路径必须是仓库或缓存根下的相对路径；拒绝绝对路径和 `..`。能找到的文件必须校验哈希；找不到或哈希不符时 release 映射为 `blocked`。

**步骤 4：加入唯一 test-only 映射**

`compat/gigadevice/gd32f103c8.json` 使用：

```json
{
  "schema": 1,
  "chip": "gd32f103c8",
  "alias": "stm32f103c8",
  "rust_target": "thumbv7m-none-eabi",
  "scope": "test",
  "source": {
    "pack": "GigaDevice.GD32F10x_DFP@2.0.3",
    "device": "GD32F103C8"
  },
  "names": {
    "peripherals": {},
    "interrupts": {},
    "signals": {}
  },
  "evidence": {},
  "patch": {}
}
```

**步骤 5：验证并提交**

```bash
rtk cargo fmt --all -- --check
rtk cargo test --test mapping
rtk git add src/mapping.rs src/merge_patch.rs tests/mapping.rs compat evidence/README.md
rtk git commit -m "功能：定义兼容映射与发布证据门"
```

---

### 任务 5：生成全量、唯一且确定的审计报告

**文件：**

- 新建：`src/report.rs`
- 修改：`src/main.rs`
- 修改：`tests/inventory.rs`
- 生成：`reports/inventory.json`

**步骤 1：先写状态闭包测试**

从 fixture lock 和映射生成报告，断言每个锁定器件恰有一条记录，状态只能是 `unmapped`、`blocked`、`ready`、`not_applicable`。重复、遗漏或未知状态都失败。

状态规则保持直接：

- Rust target 为空、非小端或不支持的多核形态：`not_applicable`；
- 无映射：`unmapped`；
- test 映射或 release 门未全过：`blocked`；
- 只有 release 且全部门通过：`ready`。

**步骤 2：实现确定性 JSON 输出**

使用结构体和 `BTreeMap`，按 `chip` 排序；输出末尾保留一个换行。`--frozen` 重新计算得到的字节与已存在报告不同就失败，并将候选结果保留在临时文件以供 diff，不覆盖报告。

**步骤 3：用完整 target DB 快照生成首版报告**

```bash
rtk cargo run -- sources update --cache-dir .cache/sources
rtk cargo run -- audit --frozen --cache-dir .cache/sources
```

预期摘要必须为：

```text
packs=12 devices=388 ready=0 blocked=1 unmapped=387 not_applicable=0
```

若实际数据不满足该闭包，停止并修复选择/规范化逻辑，禁止改期望值迁就错误结果。

**步骤 4：验证确定性并提交**

```bash
rtk cargo run -- audit --frozen --cache-dir .cache/sources
rtk cargo run -- audit --frozen --cache-dir .cache/sources
rtk git diff --exit-code -- sources.lock.toml reports/inventory.json
rtk cargo test --test inventory
rtk git add src/report.rs src/main.rs tests/inventory.rs sources.lock.toml reports/inventory.json
rtk git commit -m "数据：完成 GigaDevice 全量型号审计"
```

---

### 任务 6：只为映射器件解析 PDSC 内存与文件路径

**文件：**

- 新建：`src/pdsc.rs`
- 新建：`tests/fixtures/pdsc/mapped-device.pdsc`
- 新建：`tests/pdsc.rs`
- 修改：`src/mapping.rs`

**步骤 1：写继承 fixture 与失败测试**

fixture 把 `memory`、`compile header`、`debug svd` 分别放在 family、subFamily、device、variant 四层，并让低层覆盖一个同名 memory。断言最终只得到映射器件的有效事实，不返回其他器件。

另测：缺少设备、重复设备、路径越出 Pack 根、同名 memory 属性冲突必须失败。

**步骤 2：实现窄 PDSC 解析器**

用 `roxmltree` 只实现：

- family/subFamily/device/variant 的目标路径选择；
- `memory` 按 `id`，无 `id` 时按 `name` 覆盖；
- `compile@header`；
- `debug@svd`；
- 十六进制和十进制的内存地址/大小。

processor 不在本模块解析，继续使用 target DB，避免两套 CPU 继承规则。

**步骤 3：接入 release 映射验证**

PDSC 得到的内存、header、SVD 相对路径必须与锁文件和证据引用一致；test 映射允许缺证据但报告原因必须明确。

**步骤 4：验证并提交**

```bash
rtk cargo fmt --all -- --check
rtk cargo test --test pdsc
rtk cargo test --test mapping
rtk git add src/pdsc.rs src/mapping.rs tests/pdsc.rs tests/fixtures/pdsc
rtk git commit -m "功能：验证映射器件的 PDSC 事实"
```

---

### 任务 7：实现官方基线上的真实芯片生成

**文件：**

- 新建：`src/generate.rs`
- 新建：`tests/fixtures/upstream/chips/STM32F103C8.json`
- 新建：`tests/fixtures/upstream/registers/`
- 新建：`tests/generation.rs`
- 修改：`src/main.rs`

**步骤 1：先写输入和输出安全测试**

断言：

- 官方 checkout revision 不匹配时失败；
- 输出目录不存在或为空才允许生成；
- 非空输出目录保持字节不变；
- patch 后无法反序列化为固定 `stm32_data_serde::Chip` 时失败；
- patch 后强制把 `Chip.name` 设为真实型号，不能伪装成 alias；
- 引用不存在的 register kind/version 时失败。

**步骤 2：实现 staging 数据目录**

对所有选中映射一次完成：

1. 读取 `official-generated/data/chips/<ALIAS>.json`；
2. 应用 RFC 7396 patch；
3. 设置真实 `name`；
4. 反序列化为固定 schema；
5. 只复制这些 Chip 引用的 register JSON 到临时 `data/registers/`；
6. 一次调用 `stm32_metapac_gen::Gen` 生成所有真实芯片。

生成器 panic 必须在临时目录边界内转换为错误；最终目录在所有检查成功前不可见。

**步骤 3：验证并复用官方共享模块**

对 staging 中每个 `src/peripherals/*.rs` 与 `src/registers/*.rs` 使用锁定工具链 rustfmt，随后与官方 `stm32-metapac` 对应文件逐字节比较。缺文件或不一致立即失败；最终输出不复制这些 staging 共享文件。

**步骤 4：合并真实芯片私有文件**

先递归复制官方 `stm32-metapac` 基线，再复制：

- `src/chips/<real>/pac.rs`；
- `src/chips/<real>/metadata.rs`；
- `src/chips/<real>/device.x`；
- 被真实 metadata 引用并改名为 `compat_metadata_NNNN.rs` 的 dedup 文件。

重写 `metadata.rs` 的 include 路径时只接受生成器的精确格式；格式变化就失败，不能做模糊替换。

**步骤 5：先用 `--include-test` 生成 GD32F103C8**

```bash
rtk cargo run -- generate \
  --official-generated ../stm32-data-generated \
  --output /tmp/embassy-mcu-compat-generated-test \
  --include-test
```

预期：存在 `src/chips/gd32f103c8/{pac.rs,metadata.rs,device.x}`；默认不加 `--include-test` 时该目录不存在。

**步骤 6：验证并提交**

```bash
rtk cargo fmt --all -- --check
rtk cargo test --test generation
rtk cargo clippy --all-targets -- -D warnings
rtk git add src/generate.rs src/main.rs tests/generation.rs tests/fixtures/upstream
rtk git commit -m "功能：基于官方数据生成真实芯片 PAC"
```

---

### 任务 8：实现不改变 Cargo feature 的兼容芯片选择

**文件：**

- 修改：`src/generate.rs`
- 新建：`tests/fixtures/metapac/build.rs`
- 修改：`tests/generation.rs`

**步骤 1：写 build 脚本行为测试**

把选择逻辑生成为独立、无依赖的 Rust 文件，在测试中用 `rustc --test` 编译并覆盖：

1. 环境变量未设置：`stm32f103c8 -> stm32f103c8`；
2. `gd32f103c8` 且 alias 正确：选择 `gd32f103c8`；
3. 未知真实型号：失败并列出真实型号；
4. `gd32f103c8` 配 `stm32f103cb`：alias 不匹配失败；
5. 零个或多个 STM32 feature：保持上游失败语义；
6. 输出 `cargo:rerun-if-env-changed=EMBASSY_MCU_COMPAT_CHIP`。

**步骤 2：生成静态兼容表**

最终 `src/compat.rs` 只含 release 映射；`--include-test` 才加入 test 映射。真实型号不添加到 `Cargo.toml` features，也不修改 `src/all_chips.rs`。

**步骤 3：替换 `build.rs`，保留原生路径**

从上游 build 脚本保留“恰好一个 STM32 feature”规则，只在得到 alias 后读取环境变量并切换私有目录。`rt` 链接搜索路径、PAC 和 metadata include 路径都使用同一个最终选择结果。

**步骤 4：记录生成清单**

根目录 `generation.json` 记录全部输入 revision/hash、是否包含 test 映射、生成的真实型号和 alias。`Cargo.toml` 只改 repository/description；包名、版本、依赖和所有官方 feature 字节语义不变。

**步骤 5：验证基线差异白名单**

测试递归比较官方基线和生成结果，只允许：

- `build.rs`；
- `Cargo.toml` 的 repository/description；
- `src/compat.rs`；
- `src/chips/gd32.../`；
- `src/chips/compat_metadata_*.rs`；
- `generation.json`。

**步骤 6：验证并提交**

```bash
rtk cargo fmt --all -- --check
rtk cargo test --test generation
rtk git add src/generate.rs tests/generation.rs tests/fixtures/metapac
rtk git commit -m "功能：按环境变量选择兼容 MCU"
```

---

### 任务 9：创建并验收纯生成仓库

**文件：**

- 新建仓库：`../embassy-mcu-compat-generated/`

**步骤 1：检查目标不存在或为空**

```bash
rtk ls -la ..
```

如果目标已存在且非空，停止；不得删除或覆盖。

**步骤 2：生成 test 验证仓库**

```bash
rtk cargo run --release -- generate \
  --official-generated ../stm32-data-generated \
  --output ../embassy-mcu-compat-generated \
  --include-test
```

**步骤 3：初始化生成仓库并提交**

```bash
rtk git init ../embassy-mcu-compat-generated
rtk git -C ../embassy-mcu-compat-generated add .
rtk git -C ../embassy-mcu-compat-generated commit -m "生成：初始化 MCU 兼容 metapac"
```

不配置远端、不 push。

**步骤 4：编译 PAC 的原生与兼容路径**

```bash
rtk cargo check --manifest-path ../embassy-mcu-compat-generated/Cargo.toml \
  --target thumbv7m-none-eabi --features pac,metadata,stm32f103c8
rtk proxy env EMBASSY_MCU_COMPAT_CHIP=gd32f103c8 cargo check \
  --manifest-path ../embassy-mcu-compat-generated/Cargo.toml \
  --target thumbv7m-none-eabi --features pac,metadata,stm32f103c8
```

预期：两者均通过；第二条使用真实私有目录。执行时变量名不得持久写入用户 shell 配置。

---

### 任务 10：验证 `embassy-stm32` 零修改 Cargo patch

**文件：**

- 新建：`examples/gd32f103c8/Cargo.toml`
- 新建：`examples/gd32f103c8/.cargo/config.toml`
- 新建：`examples/gd32f103c8/src/lib.rs`
- 新建：`examples/stm32f103c8-native/Cargo.toml`
- 新建：`examples/stm32f103c8-native/src/lib.rs`

**步骤 1：创建最小 `no_std` 消费者**

两个 example 都只依赖固定 revision 的 `embassy-stm32` 并启用 `stm32f103c8`；使用：

```toml
[patch."https://github.com/embassy-rs/stm32-data-generated"]
stm32-metapac = { path = "../../../embassy-mcu-compat-generated" }
```

兼容 example 的 `.cargo/config.toml` 写入：

```toml
[env]
EMBASSY_MCU_COMPAT_CHIP = { value = "gd32f103c8", force = true }
```

Rust 源只包含 `#![no_std]` 和对 `embassy_stm32` 的引用，不伪造硬件功能测试。

**步骤 2：分别从 example 目录编译**

```bash
rtk cargo check --target thumbv7m-none-eabi
```

分别在 `examples/stm32f103c8-native` 和 `examples/gd32f103c8` 目录运行。必须从各目录运行，确保 Cargo 读取对应 `.cargo/config.toml`。

**步骤 3：证明上游仓库零修改**

```bash
rtk git -C ../stm32-data status --short
```

预期仍只有用户已有的 `?? docs/`；不允许出现本项目生成内容。

**步骤 4：提交 example**

```bash
rtk git add examples
rtk git commit -m "测试：验证 embassy-stm32 零修改兼容路径"
```

---

### 任务 11：补齐确定性、错误路径和升级门

**文件：**

- 新建：`.github/workflows/ci.yml`
- 修改：`tests/generation.rs`
- 修改：`tests/inventory.rs`
- 修改：`README.md`

**步骤 1：加入两次生成树哈希测试**

相同输入生成两个临时目录，递归树哈希必须一致。再修改一个输入 hash，生成必须在写最终目录前失败。

**步骤 2：加入错误选择 compile-fail 检查**

用临时消费 crate 覆盖未知真实型号和 alias 不匹配，断言 Cargo 失败信息包含 `EMBASSY_MCU_COMPAT_CHIP`、真实型号和期望 alias。

**步骤 3：定义 CI 的离线层与联网层**

- 每次提交：fmt、clippy、全部 fixture 单元/集成测试；
- 手工或定时任务：checkout 固定 target DB、运行 `audit --frozen`、比较 `sources.lock.toml` 与 `reports/inventory.json`；
- 不在普通 CI 自动接受 Pack EULA，不自动发布生成仓库。

**步骤 4：写升级说明**

README 只说明：架构、三条命令、缓存准备、Cargo patch 用法、test-only 限制、release 证据门、固定 revision 整体升级方式。明确 `cmsis-rust-target-db` 只提供器件/CPU 事实，不提供 Embassy pinmux/DMA/RCC metadata。

**步骤 5：全量验证并提交**

```bash
rtk cargo fmt --all -- --check
rtk cargo clippy --all-targets -- -D warnings
rtk cargo test --all-targets
rtk cargo run -- audit --frozen --cache-dir .cache/sources
rtk git diff --check
rtk git add .github README.md tests
rtk git commit -m "文档：完成生成链验收与升级说明"
```

---

### 任务 12：最终验收与边界复核

**步骤 1：核对三个工作树**

```bash
rtk git status --short
rtk git -C ../embassy-mcu-compat-generated status --short
rtk git -C ../stm32-data status --short
```

预期：两个新仓库干净；`stm32-data` 只保留执行前已有的 `?? docs/`。

**步骤 2：核对全量闭包**

从报告读取并确认：12 Pack、388 型号、`ready=0`、`blocked=1`、`unmapped=387`、`not_applicable=0`。确认 `gd32f103c8` 原因为 test-only，而不是生产可用。

**步骤 3：核对零修改与选择契约**

- 原生 `STM32F103C8` example 通过；
- 设置环境变量的 GD test example 通过；
- 生成包没有 `gd32...` Cargo feature；
- `ALL_CHIPS` 与官方基线字节一致；
- 生成清单包含全部固定 revision/hash；
- 没有原始 `.pack`、手册或厂商头文件进入 Git。

**步骤 4：记录最终提交**

只有在上述检查全部通过后，提交任何剩余的报告或说明：

```bash
rtk git add sources.lock.toml reports/inventory.json README.md
rtk git commit -m "发布：完成首版 GigaDevice 全量审计"
```

若没有剩余变更，不创建空提交。

## 明确不在首版实现的内容

- 不声明任何 GD 型号已达到生产 `ready`；需要真实手册证据和硬件冒烟后逐个开放。
- 不修改或重构 `cmsis-rust-target-db`；现有 JSONL 已满足器件发现需求。
- 不创建厂商 plugin、数据库服务、Web UI、自定义 DSL 或真实型号 Cargo feature。
- 不为官方共享模块生成新 register version；出现新版本时先证明 `embassy-stm32` 已有对应驱动，否则保持 `blocked`。
