# `embassy-mcu-compat` 设计规格

- 日期：2026-08-12
- 状态：已批准；已纳入 `cmsis-rust-target-db` 数据源
- 首个厂商：GigaDevice（兆易创新）

## 1. 目标

本项目为 `embassy-stm32` 提供厂商无关的 MCU 兼容数据生成路径。应用仍选择一个与真实芯片兼容的 STM32 feature；Cargo 将 `stm32-metapac` patch 到本项目的生成仓库；被 patch 的 PAC 再根据真实型号选择真实 metadata、PAC 和中断向量。

设计必须同时满足以下不变量：

1. `embassy-stm32` 零修改。
2. `embassy-rs/stm32-data` 零修改，只作为固定 revision 的只读生成器来源。
3. `embassy-rs/stm32-data-generated` 零修改，只作为与生成器配对的只读数据及 PAC 基线。
4. 生成器、输入格式和审计逻辑不含 `if vendor == "GigaDevice"` 一类厂商分支。
5. GigaDevice 是首个正式接入的国产厂商；首版完整枚举和审计官方索引中的全部 GigaDevice DFP 及其中全部叶子器件。
6. 未经验证的相似性不得生成生产支持。每个器件必须有明确审计状态，不能被静默忽略。
7. `GD32F103C8 -> STM32F103C8` 只用于测试和 example，不构成生产兼容声明。

首版的“全覆盖”指完整的数据源闭包和状态闭包，不表示所有 GigaDevice 器件在首版都被标记为可生产使用。只有满足全部兼容门槛的映射才进入发布生成物。

## 2. 可行性结论

方案可行，但 CMSIS Device Family Pack 只能提供生成链的一部分事实：

- [`cmsis-rust-target-db`](https://github.com/itswenb/cmsis-rust-target-db) 已经把 PDSC 的器件层次、处理器继承和 Rust target 归一化为可复现 JSONL，可直接承担全量型号发现和 CPU/ABI 初筛。
- 原始 PDSC 仍用于已映射器件的内存、头文件和 SVD 路径验证。
- SVD 和厂商头文件可以提供寄存器结构、外设基址及中断编号。
- DFP 通常不能完整表达 Embassy 所需的 pinmux、DMA 请求、RCC 时钟树、Flash 操作语义和 HAL 分支兼容性。

因此不能直接把 `cmsis-rust-target-db` 或 DFP 转成完整 `stm32-data`。正确路径是：以前者作为厂商无关的器件清单与 CPU 事实来源，以一个显式 STM32 alias 的 `stm32-data-serde::Chip` 为基线，用厂商 Pack 和手册验证并修正真实差异，再复用上游 `stm32-metapac-gen` 生成真实芯片目录。

CMSIS-Toolbox 的 `cpackget` 负责获取、缓存和解包 Pack，不承担 Embassy metadata 转换。普通审计和生成均离线运行，只有显式的来源更新命令访问网络。

## 3. 首版官方来源闭包

首版锁定 Keil 公共 Pack 索引快照：

- URL：`https://www.keil.com/pack/index.pidx`
- 索引时间戳：`2026-08-12T04:05:17.3114066+00:00`
- SHA-256：`136ec2208d31b5d0e8697a806e40d5647799c8c097ae283f28712079dc7b2e81`

该快照中 `vendor="GigaDevice"` 的全部 12 个 DFP 为：

| Pack | 版本 |
|---|---:|
| `GD32F10x_DFP` | `2.0.3` |
| `GD32F1x0_DFP` | `3.2.1` |
| `GD32F20x_DFP` | `2.2.3` |
| `GD32F30x_DFP` | `2.2.1` |
| `GD32F3x0_DFP` | `3.0.2` |
| `GD32F4xx_DFP` | `3.0.3` |
| `GD32C10x_DFP` | `1.0.1` |
| `GD32E10x_DFP` | `1.2.1` |
| `GD32E23x_DFP` | `1.0.2` |
| `GD32E50x_DFP` | `1.3.2` |
| `GD32L23x_DFP` | `1.0.3` |
| `GD32W51x_DFP` | `1.0.3` |

初始上游配对为：

- `embassy`：`98d847be57f3ea022ce05fe9b95ab3639a1e0a93`
- `stm32-data`：`87c539515764df442bc50b6235bad891950ba3c4`
- `stm32-data-generated`：`12ec4cd38c7825c1ff8592de1bdefaae445bb3a6`
- 后者提交信息明确记录由前者生成。
- `chiptool`：由该 `stm32-data` revision 固定的 `be1bff3e9e1b27b090e69bd9ac753c66fdcce678`。

首版同时锁定现有 `cmsis-rust-target-db` 生成快照：

- revision：`947e8a96d462801e827ed76408c8d8457326f6a1`；
- `data/devices.jsonl` SHA-256：`114e695810217c84943b9d61ab385e3fe10f850cc914f96aac280a509c650c88`；
- `data/metadata.json` SHA-256：`aa9b6fc936b1fd615c07fdcd9dddafe2a902b5a923ac50125decc4927693233f`；
- schema 版本 `1`，共 `14,436` 条记录、`1,477` 个 PDSC，记录的索引 SHA-256 与本节锁定索引完全一致；
- 其中 GigaDevice 恰好覆盖上述 12 个 DFP 的 `388` 个器件记录，全部具有可解析 Rust target。

该仓库只作为固定 revision 的只读生成数据来源；首版不修改它、不把它变成库依赖，也不重复实现它已经完成的处理器继承和 target 推导。

上述值写入 `sources.lock.toml`。更新来源时必须整体更新并重新验证配对关系，不能单独漂移其中一个 revision。

## 4. 仓库边界

使用两个新仓库：

### 4.1 `embassy-mcu-compat`

这是唯一的手工维护仓库，包含：

- 单一 Rust CLI 包 `mcu-compat-gen`；
- `sources.lock.toml`；
- `compat/<vendor>/<chip>.json` 显式兼容映射；
- `reports/inventory.json` 全量审计结果；
- 测试 fixture、example 和设计文档。

它读取固定 revision 的 `cmsis-rust-target-db/data/*.json*`，但不复制或分叉其生成器代码。

首版不创建 workspace、vendor plugin、独立 metadata crate、自定义 patch DSL 或数据库。

### 4.2 `embassy-mcu-compat-generated`

这是纯生成仓库，仓库根目录就是包名仍为 `stm32-metapac` 的 Cargo 包。它包含：

- 官方 `stm32-data-generated/stm32-metapac` 的只读复制基线；
- 通过生产门槛的真实芯片私有目录；
- 兼容芯片选择表和替换后的 `build.rs`；
- 可追溯输入 revision 与哈希的生成清单。

它不包含原始 Pack、手册、厂商头文件或完整上游 `data/`，避免无必要的仓库体积和再分发风险。

## 5. 命令与数据流

CLI 只提供三个顶层动作：

### 5.1 `sources update`

这是唯一联网动作：

1. 使用 `cpackget` 更新指定公共索引。
2. 读取固定 revision 的 `cmsis-rust-target-db` 数据和元数据；其 `source_index_sha256` 必须等于本次索引哈希，否则立即失败，禁止混用不同快照。
3. 从规范化数据中选出来源登记匹配的全部 Pack 和器件，而不是维护手写系列名单。来源登记由通用的 `--vendor` 和 `--pack-pattern` 参数写入锁文件。
4. 下载并解包精确版本的 Pack。
5. 计算索引、数据快照、Pack、PDSC、SVD、相关头文件和许可证文件的 SHA-256。
6. 将 JSONL 记录规范化为审计器件：有 variant 的父 device 不单独计数，variant 和无 variant 的 device 保留；同一 Pack、同一器件的多处理器记录合并为一个器件及处理器列表。
7. 更新 `sources.lock.toml`。

初始登记等价于 `--vendor GigaDevice --pack-pattern '*_DFP'`；以后接入其他厂商只增加来源登记和兼容数据，不修改解析或生成代码。后续更新默认复用锁文件中的登记条件。

工具不自动接受 EULA。若某 Pack 要求显式接受许可证，必须由调用者明确授权后再传给 `cpackget`。

### 5.2 `audit --frozen`

这是离线动作：

1. 校验本地 Pack 与锁文件哈希。
2. 校验 `cmsis-rust-target-db` 数据文件哈希、schema、索引哈希及锁定 Pack/器件闭包；只为存在兼容映射的器件解析 PDSC 所引用的内存和文件路径。
3. 校验兼容映射及证据。
4. 计算每个规范化审计器件的唯一审计状态。
5. 确定性生成 `reports/inventory.json`。

`--frozen` 下任何来源漂移都会失败。`unmapped` 本身不是程序错误，因为它是有效审计结果。

### 5.3 `generate`

这是离线动作，只消费锁定输入和已通过审计的映射：

1. 校验 `stm32-data` 与 `stm32-data-generated` revision 配对。
2. 从官方 generated checkout 复制 `stm32-metapac` 到一个全新空输出目录。
3. 对每个允许生成的真实芯片，读取 alias 的 `Chip` JSON，应用显式 JSON Merge Patch，并反序列化回上游 `Chip` 类型验证 schema。
4. 在临时数据目录中调用固定 revision 的 `stm32-metapac-gen::Gen`，且只生成真实芯片。
5. 验证临时生成的每个共享 peripheral/register 模块均已存在于官方基线且字节一致。
6. 只把 `src/chips/<real-chip>/` 复制到最终输出；丢弃临时 Cargo features、`ALL_CHIPS` 和共享模块。
7. 写入兼容选择表、自定义 `build.rs` 和生成清单；Cargo manifest 只修改 `package.repository` 与 `package.description` 等描述性字段，包名、版本和全部 features 保持官方基线。

这种“官方基线 + 私有芯片目录覆盖”比全量重生成更小，也保证原生 STM32 的 Cargo features、`ALL_CHIPS`、PAC 和 metadata 均保持官方内容。

## 6. 来源锁与兼容映射

### 6.1 `sources.lock.toml`

锁文件至少记录：

- schema 版本；
- 公共索引 URL、时间戳和哈希；
- 已登记的 Pack vendor；
- 每个 Pack 的 ID、版本、URL 和哈希；
- Pack 内 PDSC、SVD、头文件和许可证入口的路径及哈希；
- 全部规范化审计器件及其原始名称；
- `cmsis-rust-target-db` revision、schema、文件哈希和记录筛选规则；
- `embassy`、`stm32-data`、`stm32-data-generated` 和 `chiptool` revision；
- `stm32-metapac` 包版本。

锁文件不保存下载缓存路径，也不依赖某台机器的绝对路径。

### 6.2 `compat/*.json`

兼容映射采用 JSON，因为最终转换对象本身就是 `stm32-data-serde::Chip` JSON。首版不再引入第二套配置语言。

概念结构如下：

```json
{
  "schema": 1,
  "chip": "gd32f103c8",
  "alias": "stm32f103c8",
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

规则如下：

- `chip` 是环境变量使用的规范小写真实型号。
- `alias` 必须是固定上游 revision 已存在的 STM32 chip-core 名称。
- `scope` 只有 `test` 和 `release`；`test` 永不进入发布生成物。
- `names` 仅描述真实命名与 STM32 语义命名之间的显式对应，不进行相似度猜测。
- `evidence` 按 `cpu`、`memory`、`interrupts`、`registers`、`rcc`、`flash`、`pins`、`dma`、`alias_cfg` 和 `license` 分类，引用锁定文件、手册定位或硬件测试记录。
- `patch` 遵循 RFC 7396 JSON Merge Patch；对象可以局部修改，数组必须整体替换，`null` 表示删除继承但真实芯片不存在的字段。

`GD32F103C8 -> STM32F103C8` fixture 使用 `scope = "test"`、空差异 patch，用于证明完整管线，不作为真实硬件兼容证据。

## 7. 审计状态与生成门槛

每个锁定审计器件必须且只能拥有以下一个状态：

- `unmapped`：没有 release 兼容映射。
- `blocked`：存在映射，但证据、许可证、验证或生成条件不完整；test-only 映射也属于此状态并注明原因。
- `ready`：全部生产门槛通过，可进入发布生成物。
- `not_applicable`：CPU、ABI、字节序或其他基础条件不可能走 `embassy-stm32` 兼容路径。

生产 `ready` 必须同时满足：

1. Pack、版本、器件名和全部输入哈希与锁文件一致。
2. alias 存在于固定上游数据，且真实 CPU、ABI、字节序、FPU 与 Rust target 兼容。
3. 真实 Flash/RAM 地址和大小已验证，未盲目继承 alias。
4. 保留外设的基址、中断编号和寄存器布局已自动验证或具有明确人工证据。
5. RCC、PWR、FLASH、GPIO、EXTI 以及所选时基的初始化语义已验证。
6. pinmux、DMA 请求和时钟差异已进入真实 metadata；不兼容外设已删除。
7. 每个寄存器 kind/version 都是上游现有版本，生成结果与官方共享模块字节一致。
8. alias 名称触发的 `embassy-stm32` family/package/flash-size cfg 和硬编码分支适用于真实芯片。
9. Pack 内容和生成物具有可接受的再分发依据；许可证未知时只能本地审计，不能发布。
10. 真实 metadata 同时通过 PAC、`embassy-stm32` build script 和目标 Rust target 编译检查。
11. 对生产支持声明所涉及的基本初始化、GPIO、中断、时基和至少一个串口完成硬件冒烟记录；同一兼容类的覆盖范围必须在证据中明确。

不存在的新寄存器版本不会在首版引入。零修改 `embassy-stm32` 时没有对应驱动，提前生成这种版本没有价值。

## 8. Cargo 与芯片选择契约

应用继续声明 STM32 alias：

```toml
[dependencies]
embassy-stm32 = { version = "...", features = ["stm32f103c8", "time-driver-any"] }

[patch."https://github.com/embassy-rs/stm32-data-generated"]
stm32-metapac = { git = "https://.../embassy-mcu-compat-generated", rev = "..." }
```

如果所用 `embassy-stm32` 来自 crates.io，patch 表必须改为匹配其实际依赖源的 `[patch.crates-io]`。生成包名和兼容版本保持为 `stm32-metapac`。

真实型号由应用配置：

```toml
# .cargo/config.toml
[env]
EMBASSY_MCU_COMPAT_CHIP = { value = "gd32f103c8", force = true }
```

生成的 `stm32-metapac/build.rs` 行为为：

1. 沿用上游规则，要求恰好启用一个 STM32 chip feature。
2. 未设置 `EMBASSY_MCU_COMPAT_CHIP` 时，选择该原生 STM32 目录。
3. 设置变量时，真实型号必须存在于生成选择表，且其 alias 必须等于已启用的 STM32 feature。
4. 匹配后将 PAC、metadata 和 `rt` 链接搜索路径切换到真实芯片目录。
5. 输出 `cargo:rerun-if-env-changed=EMBASSY_MCU_COMPAT_CHIP`，避免 Cargo 缓存错误选择。

真实型号不作为 Cargo feature，不加入 `ALL_CHIPS`。这是必要条件：`embassy-stm32` 会把 `ALL_CHIPS` 当成 STM32 命名解析，并通过 alias 名称选择现有 family cfg；真实 metadata 则由被 patch 的 PAC 提供。

同一次 Cargo 调用只允许一个真实型号。需要同时构建多个真实 MCU 时，应分别调用 Cargo 并使用独立配置或环境；首版不为此增加额外选择机制。

## 9. PDSC、SVD 与许可证处理

- 全量器件层次、processor 继承和 Rust target 直接消费固定的 `cmsis-rust-target-db` 数据，不在本仓库重复解析。
- PDSC 使用轻量 XML 树解析，只实现已映射器件验证需要的 `memory`、`compile/debug`、SVD/header 路径继承，不复制完整 CMSIS schema。
- SVD 解析和规范化复用固定 revision 的 `chiptool`/`svd-parser`，不编写第二套寄存器 IR。
- pinmux、DMA、RCC 和 Flash 语义没有可靠 Pack 来源时必须依赖显式证据和 patch。
- 原始 Pack 仅存在于忽略的本地缓存，不进入两个 Git 仓库。
- 许可证识别只记录事实，不自动作法律判断。缺少明确再分发许可时，发布门槛失败并给出原因。

已检查的 `GD32F10x_DFP 2.0.3` 没有顶层许可证文件，厂商头文件包含 BSD 三条款文本，但 SVD 未带同等声明。因此不能仅凭头文件许可证自动推导整个 Pack 的派生数据均可再分发。

## 10. 错误和写入安全

以下情况返回非零状态，且不得留下可发布的部分输出：

- 索引、Pack 或内部文件哈希不匹配；
- 数据快照/PDSC/SVD 解析失败、设备继承不完整或审计器件重复；
- 映射引用不存在的 Pack、device、alias 或寄存器版本；
- patch 后不能反序列化为上游 `Chip`；
- alias 与环境选择不一致；
- 生成的共享模块与官方基线不同；
- release 映射缺少证据或许可证门槛；
- 输出目录已非空。

`unmapped`、`blocked`、`not_applicable` 是合法审计结果，不导致 `audit` 失败。`generate` 只处理 `ready`，测试模式只处理显式 `scope = "test"`。

生成器是单一应用 CLI，使用带上下文的应用级错误；首版不建立大型公共错误枚举。输出必须写入调用者提供的全新空目录，生成器不负责删除或覆盖已有仓库。

## 11. 验证策略

### 11.1 最小单元测试

1. `cmsis-rust-target-db` fixture 覆盖 variant 去父项、多处理器合并、Pack/version 筛选和索引哈希不一致失败。
2. PDSC fixture 只覆盖已映射器件的 memory 与 SVD/header 路径继承。
3. RFC 7396 patch 覆盖对象合并、数组整体替换和 `null` 删除。
4. inventory 保证每个审计器件恰好一个状态且无重复。
5. build 选择逻辑覆盖：无环境变量、正确映射、未知型号、alias 不匹配、零个和多个 STM32 feature。

### 11.2 集成测试

1. 固定 `cmsis-rust-target-db` 快照必须得到上述 12 个 GigaDevice DFP 和全部 `388` 个审计器件。
2. `audit --frozen` 在同一输入上生成字节一致的报告。
3. 仅官方 STM32 基线时，除允许替换的 `build.rs`、兼容表、生成清单和包描述外，其余文件与官方生成物字节一致。
4. 每个真实芯片临时生成的共享 peripheral/register 文件与官方基线字节一致。
5. 未设置环境变量时，代表性 STM32 alias 可以正常编译，证明原生路径未回归。
6. 测试模式下，`GD32F103C8 -> STM32F103C8` 能同时驱动普通依赖和 build-dependency 的真实 metadata 选择，并通过 `embassy-stm32` example 的目标编译。
7. 未知真实型号和错误 alias 必须在编译期给出明确失败。
8. 两次完整生成得到相同文件树哈希。

### 11.3 上游升级门

更新任一上游 revision 时必须重新运行全部测试。若官方 `stm32-metapac` 模板、metadata schema、Cargo feature 传播或 `embassy-stm32` build script 契约发生变化，升级失败并要求显式适配，不能自动发布。

## 12. 首版完成标准

首版在同时满足以下条件时完成：

1. 新源仓库和新生成仓库可从空目录确定性构建。
2. `embassy-stm32`、`stm32-data` 和官方 `stm32-data-generated` 均无任何提交或工作树修改。
3. 锁文件包含官方快照中的全部 12 个 GigaDevice DFP。
4. 审计报告包含这些 Pack 内全部 `388` 个器件，且每个器件恰好一个状态和明确原因。
5. 架构、schema、CLI 和生成路径接入下一厂商时不需要修改厂商分支代码。
6. 原生 STM32 无环境变量路径通过回归测试。
7. test/example 中的 `GD32F103C8 -> STM32F103C8` 完整通过 Cargo patch、metadata、PAC 和 `embassy-stm32` 编译链。
8. 发布生成物只包含达到 `ready` 的生产映射；如果首版尚无完成全部证据的生产映射，发布集合可以为空，但全量 GigaDevice 审计和通用生成能力必须完整。

## 13. 参考来源

- [Open-CMSIS-Pack PDSC 设备族规范](https://open-cmsis-pack.github.io/Open-CMSIS-Pack-Spec/main/html/pdsc_family_pg.html)
- [CMSIS-Toolbox `cpackget`](https://open-cmsis-pack.github.io/devtools/buildmgr/latest/cpackget.html)
- [`itswenb/cmsis-rust-target-db`](https://github.com/itswenb/cmsis-rust-target-db)
- [`embassy-rs/stm32-data`](https://github.com/embassy-rs/stm32-data)
- [`embassy-rs/stm32-data-generated`](https://github.com/embassy-rs/stm32-data-generated)
- [`embassy-rs/chiptool`](https://github.com/embassy-rs/chiptool)
- [`embassy-rs/embassy/embassy-stm32`](https://github.com/embassy-rs/embassy/tree/main/embassy-stm32)
