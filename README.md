# embassy-mcu-compat

本仓库为 Embassy 构建厂商无关的 MCU 数据与兼容生成链，GigaDevice 是第一个接入厂商。官方 `embassy-stm32`、`stm32-data` 和 `stm32-data-generated` 始终保持零修改。

当前已建立 GD32 官方来源的可重复同步和缺口闭包：680 个规范型号均有寄存器来源并生成原生 PAC，其中 48 个 A7xx 型号来自 IAR 官方 Arm 设备支持包中的 3 份 SVD。尚未声明任何 GD32 型号达到 Embassy 生产支持；仓库中的 `GD32F103C8 -> STM32F103C8` 仅是 Cargo patch 端到端测试。

## 当前覆盖证据

- 33 个官方 Firmware Library；
- 30 个官方 AddOn，得到 29 个最新 CMSIS Pack；
- 680 个规范 device、701 个完整料号和 782 个目录条目；
- 598 个唯一 CMSIS 设备、80 个原始 SVD、43 个 PDSC 实际引用的唯一 SVD；
- Embedded Builder 中 26 个 AFIO XML、227 个封装/管脚 XML；
- 2025 中英文 Selection Guide 中的型号已经与 Pack、Firmware、Builder 和 Programmer 闭合；
- 33 个宽松许可 Firmware 中心头文件全部完成中断/基址索引；
- 850 个唯一宽松许可寄存器头文件提取出 15,738 个寄存器和 59,599 个位域；
- 71 个由真实预处理条件生成的 Firmware 变体覆盖 632 个 device；3 个 H767 型号由 CMSIS Pack 的头文件、SVD 和编译宏签名安全补全；
- IAR 9.70.4 官方设备支持包补齐 48 个 A7xx 型号：33 个精确 DDF 加 15 个同芯片 B 封装变体，共映射到 3 份 SVD；
- 71 份 Firmware PAC 候选全部通过 `chiptool` 和独立 Rust 类型检查，共 103,135 个寄存器、505,893 个位域和 3,000 个已闭合数组；
- 632 个 Firmware Chip 加 48 个 IAR A7 Chip 已合并为 680 个原生 PAC feature；
- pins、内存、Flash、RCU 和 DMA 分别完成 599、659、625、680、632 个 device 的归一化；
- 632 个设备的原生 0 基实例名已经审计，其中 3,389 个实例可无歧义转换为 Embassy/ST 名称，家族相关名称继续保持阻塞而不猜测；
- 43 份 PAC 输出全部通过独立类型检查，映射到 598 个 device；
- 4 份 SVD 与 Firmware 中断事实冲突，影响 19 个 device，已按双侧哈希锁定并阻塞状态提升。

机器可读证据见：

- [`reports/gigadevice-catalog.json`](reports/gigadevice-catalog.json)
- [`reports/gigadevice-models.json`](reports/gigadevice-models.json)
- [`reports/gigadevice-iar-a7.json`](reports/gigadevice-iar-a7.json)
- [`reports/gigadevice-iar-svd-audit.json`](reports/gigadevice-iar-svd-audit.json)
- [`reports/gigadevice-iar-pac-compile.json`](reports/gigadevice-iar-pac-compile.json)
- [`reports/gigadevice-builder-models.json`](reports/gigadevice-builder-models.json)
- [`reports/gigadevice-pack-resources.json`](reports/gigadevice-pack-resources.json)
- [`reports/gigadevice-svd-audit.json`](reports/gigadevice-svd-audit.json)
- [`reports/gigadevice-pac-compile.json`](reports/gigadevice-pac-compile.json)
- [`reports/gigadevice-firmware-headers.json`](reports/gigadevice-firmware-headers.json)
- [`reports/gigadevice-firmware-registers.json`](reports/gigadevice-firmware-registers.json)
- [`reports/gigadevice-firmware-variants.json`](reports/gigadevice-firmware-variants.json)
- [`reports/gigadevice-firmware-pac-compile.json`](reports/gigadevice-firmware-pac-compile.json)
- [`reports/gigadevice-embassy-names.json`](reports/gigadevice-embassy-names.json)
- [`reports/gigadevice-metapac-compile.json`](reports/gigadevice-metapac-compile.json)
- [`reports/gigadevice-svd-header-comparison.json`](reports/gigadevice-svd-header-comparison.json)
- [`reports/gigadevice-mcu-data.json`](reports/gigadevice-mcu-data.json)
- [`reports/gigadevice-source-coverage.json`](reports/gigadevice-source-coverage.json)
- [`reports/embassy-stm32-boundary.json`](reports/embassy-stm32-boundary.json)

## 为什么不能只 patch metadata

metadata-only patch 仍会保留给严格验证兼容的 Cortex-M 型号，但它不能覆盖全部 GD32：`embassy-stm32` 无条件依赖 Cortex-M，并包含大量 STM32 家族 cfg/芯片名前缀分支；GD32VF103 和 GD32VW55x 则是 RISC-V。

最终采用分层路径：

- 兼容 Cortex-M：生成 `stm32-metapac` patch，继续使用未修改的 `embassy-stm32`；
- 非兼容 Cortex-M 与 RISC-V：使用厂商无关数据层和原生 backend；
- 所有官方目录型号进入同一闭包报告，缺少公开技术资料的型号明确阻塞，绝不猜测。

## 完整来源同步

所有下载均进入仓库内的 `.cache/`。GigaDevice 的三个协议必须由调用者显式接受：

```bash
GIGADEVICE_ACCEPT_SLA_GD0001=1 \
GIGADEVICE_ACCEPT_SLA_GD0003=1 \
GIGADEVICE_ACCEPT_SLA_GD0006=1 \
scripts/sync-gigadevice-sources.sh
```

不传参数时脚本把 `cmsis-rust-target-db` 最新 HEAD 锁定到项目内缓存；也可传入一个干净的本地仓库以复现指定提交。

统一脚本会依次：

1. 按 `sources.lock.toml` 锁定 Embassy、stm32-data、generated 和 chiptool；
2. 发现、下载、校验并安全解包 Firmware/AddOn/Builder/Selection Guide；
3. 用 `cmsis-rust-target-db` 解析全部 PDSC，并筛选每个 Pack 的最新版本；
4. 生成确定性型号与覆盖报告；
5. 批量生成并类型检查 SVD PAC，并独立生成 Firmware PAC；
6. 用本地 C 预处理器按真实设备选择宏生成 71 个条件 Firmware 变体；
7. 从条件 IR 生成 71 份 Firmware PAC 候选并执行 Rust 类型检查；
8. 对照 Firmware 中断/基址并应用哈希锁定的冲突门；
9. 生成 680 个规范 device 的统一 `mcu-data` 状态清单；
10. 扫描 `embassy-stm32` 架构边界并运行脚本测试。

重复运行会校验 URL、Content-Length、SHA-256、内层归档哈希和解包目录树哈希后复用缓存。原始厂商归档、XML、PDF、PDSC 和 SVD 不提交到 Git。

## 开发验证

完整验证只调用可重复脚本：

```bash
GIGADEVICE_ACCEPT_SLA_GD0001=1 \
GIGADEVICE_ACCEPT_SLA_GD0003=1 \
GIGADEVICE_ACCEPT_SLA_GD0006=1 \
scripts/verify-gigadevice.sh
```

Rust CLI 负责生成零修改 `embassy-stm32` 所需的测试 patch；`embassy-mcu-compat-generated` 同时发布由真实 GD32 数据生成的 `mcu-metapac`。当前发布树包含 680 个原生 feature；这仍不等于 Embassy HAL 或实机验证。

## 原生 PAC release

生成仓库中的 `mcu-metapac` 包包含当前可由公开来源生成的 680 个 GD32 型号。依赖时只选择一个真实型号：

```toml
[dependencies]
mcu-metapac = { git = "https://github.com/itswenb/embassy-mcu-compat-generated", rev = "<固定提交>", features = ["gd32f103c8", "pac", "metadata"] }
```

这条路径提供原生寄存器 PAC 和 metadata，不通过相似 STM32 型号伪装，也不宣称对应型号已通过 `embassy-stm32` 驱动或硬件测试。

## 测试用 Cargo patch

生成仓库根包继续用于验证原生 STM32 路径与 test-only GD32F103C8 选择逻辑：

```toml
[dependencies]
embassy-stm32 = { version = "...", features = ["stm32f103c8"] }

[patch."https://github.com/embassy-rs/stm32-data-generated"]
stm32-metapac = { git = "https://github.com/itswenb/embassy-mcu-compat-generated", rev = "<固定提交>" }
```

真实 GD32 生产映射只有在寄存器、中断、内存、RCC、Flash、pins、DMA、Embassy cfg、许可证、目标编译和硬件验证全部通过后才会进入生成仓库。
