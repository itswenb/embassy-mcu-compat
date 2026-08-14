import importlib.util
import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "build_gigadevice_firmware_variants.py"
SPEC = importlib.util.spec_from_file_location(
    "build_gigadevice_firmware_variants", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FirmwareVariantTests(unittest.TestCase):
    def test_include目录包含模板但排除示例工程(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "Firmware/Include/device.h",
                "Firmware/CMSIS/core_cm33.h",
                "Template/device_libopt.h",
                "Examples/Demo/device_libopt.h",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "#ifndef __CORE_CM33_H_GENERIC\n#ifndef __CORE_CM33_H_DEPENDANT\n"
                    if "core_" in path.name
                    else "#if defined(GD32E50X)\n#endif /* GD32E50x */\n"
                    "#include \"gd32e50x.h\"\n#error \"GD32E50x gd32e50x\"\n"
                    if path.name == "device.h"
                    else "",
                    encoding="utf-8",
                )

            result = [path.relative_to(root).as_posix() for path in MODULE.include_directories(root)]

            guards = MODULE.cmsis_guard_defines(root)
            spellings = MODULE.selector_spellings(root)

        self.assertEqual(result, ["Firmware/CMSIS", "Firmware/Include", "Template"])
        self.assertEqual(
            guards, ["__CORE_CM33_H_DEPENDANT", "__CORE_CM33_H_GENERIC"]
        )
        self.assertEqual(spellings["gd32e50x"], ["GD32E50X"])

    def test_只为固件引用但归档缺失的Core头生成shim清单(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header = root / "Firmware" / "device.h"
            header.parent.mkdir(parents=True)
            header.write_text('#include "core_cm33.h"\n', encoding="utf-8")

            missing = MODULE.missing_core_headers(root)
            shim_dir = MODULE.ensure_core_shims(root / "shims", missing)

            self.assertEqual(missing, ["core_cm33.h"])
            self.assertEqual(
                (shim_dir / "core_cm33.h").read_text(encoding="utf-8"),
                "/* 由脚本生成：厂商固件缺失的 CMSIS Core 占位头，仅用于宏预处理。 */\n",
            )

    def test_选择器扫描包含预处理续行(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header = root / "device.h"
            header.write_text(
                "#if defined(GD32H77EXP) || \\" + "\n    defined(GD32H77RXP)\n#endif\n",
                encoding="utf-8",
            )

            spellings = MODULE.selector_spellings(root)

        self.assertEqual(spellings["gd32h77rxp"], ["GD32H77RXP"])

    def test_按已锁定哈希定位器件头且结果确定(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("z/device.h", "a/device.h"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("typedef int device_header;\n", encoding="utf-8")
            sha256 = hashlib.sha256(
                (root / "a/device.h").read_bytes()
            ).hexdigest()

            result = MODULE.find_device_header(root, sha256)

        self.assertEqual(result, {"path": "a/device.h", "sha256": sha256})

    def test_只保留原始头文件声明且当前分支生效的宏(self):
        original = """\
#define USART0 USART_BASE
#define USART_STAT(usartx) REG32((usartx) + 0x00U)
#if DEVICE_A
#define USART_STAT_PERR BIT(0)
#endif
"""
        preprocessed = """\
#define __STDC__ 1
#define USART0 USART_BASE
#define USART_STAT(usartx) REG32((usartx) + 0x00U)
#define USART_STAT_PERR BIT(0)
#define USART_BASE 0x40000000U
"""

        self.assertEqual(
            MODULE.active_definitions(original, preprocessed),
            "#define USART0 USART_BASE\n"
            "#define USART_STAT(usartx) REG32((usartx) + 0x00U)\n"
            "#define USART_STAT_PERR BIT(0)\n",
        )

    def test_当前分支宏保留原始同行范围注释(self):
        original = """\
#define ESC 0x40000000U
#define FMMU_ACTIVE(x) REG8(ESC + 0x60CU + 0x10U * (x)) /*!< x = 0...7 */
"""
        preprocessed = """\
#define FMMU_ACTIVE(x) REG8(ESC + 0x60CU + 0x10U * (x))
#define ESC 0x40000000U
"""

        self.assertEqual(
            MODULE.active_definitions(original, preprocessed),
            "#define ESC 0x40000000U\n"
            "#define FMMU_ACTIVE(x) REG8(ESC + 0x60CU + 0x10U * (x)) /*!< x = 0...7 */\n",
        )

    def test_设备头预处理结果只保留外部中断(self):
        source = """\
typedef enum IRQn {
    NonMaskableInt_IRQn = -14,
    TIMER0_IRQn = 25,
    TIMER0_UP_IRQn = 25,
    USART0_IRQn = 37,
} IRQn_Type;
"""

        self.assertEqual(
            MODULE.active_interrupts(source),
            [
                {"name": "TIMER0", "value": 25},
                {"name": "TIMER0_UP", "value": 25},
                {"name": "USART0", "value": 37},
            ],
        )

    def test_解析当前分支的rcu使能与复位位(self):
        source = """
typedef enum {
    IDX_AHBEN = 0x14U,
} rcu_register_index_enum;
typedef enum {
    RCU_DMA0 = (((unsigned int)(IDX_AHBEN) << 6) | (unsigned int)(0U)),
    RCU_GPIOA = (((unsigned int)(0x18U) << 6) | (unsigned int)(2U)),
} rcu_periph_enum;
typedef enum {
    RCU_SRAM_SLP = (((unsigned int)(0x14U) << 6) | (unsigned int)(2U)),
} rcu_periph_sleep_enum;
typedef enum {
    RCU_GPIOARST = (((unsigned int)(0x0CU) << 6) | (unsigned int)(2U)),
} rcu_periph_reset_enum;
"""

        self.assertEqual(
            MODULE.parse_rcu_facts(source),
            {
                "enable": [
                    {"name": "DMA0", "register_offset": 0x14, "bit": 0},
                    {"name": "GPIOA", "register_offset": 0x18, "bit": 2},
                ],
                "reset": [
                    {"name": "GPIOA", "register_offset": 0x0C, "bit": 2}
                ],
            },
        )

    def test_解析当前分支的dmamux请求与通道(self):
        macros = """\
#define DMA_REQUEST_ADC RM_CHXCFG_MUXID(5U)
#define DMA_REQUESR_SSTAT0 RM_CHXCFG_MUXID(12U)
"""
        source = """\
typedef enum {
    DMAMUX_MULTIPLEXER_CH0 = 0,
    DMAMUX_MUXCH1,
} dmamux_multiplexer_channel_enum;
typedef enum {
    DMA_CH0 = 0,
    DMA_CH1,
    DMA_CH2,
} dma_channel_enum;
#define DMAMUX_RG_CH0CFG 0U
#define DMAMUX_RG_CH1CFG 1U
#define DMAMUX_RG_CH2CFG 2U
"""

        self.assertEqual(
            MODULE.parse_dma_facts(macros, source),
            {
                "kind": "dmamux",
                "dma_channels": [0, 1, 2],
                "dmamux_channels": [0, 1],
                "dmamux_generator_channels": [0, 1, 2],
                "requests": [
                    {
                        "name": "ADC",
                        "request": 5,
                        "source_name": "DMA_REQUEST_ADC",
                    },
                    {
                        "name": "SSTAT0",
                        "request": 12,
                        "source_name": "DMA_REQUESR_SSTAT0",
                    },
                ],
            },
        )
        self.assertEqual(
            MODULE.parse_dma_facts("", ""),
            {
                "kind": "fixed",
                "dma_channels": [],
                "dmamux_channels": [],
                "dmamux_generator_channels": [],
                "requests": [],
            },
        )

    def test_用dmamux拓扑补齐寄存器数组范围(self):
        registers = [
            {
                "name": "DMAMUX_RM_CHXCFG",
                "offset": 0,
                "width": 32,
                "array_parameters": {"channel": {"start": 0, "stride": 4}},
            },
            {
                "name": "DMAMUX_RG_CHXCFG",
                "offset": 0x100,
                "width": 32,
                "array_parameters": {"channel": {"start": 0, "stride": 4}},
            },
            {
                "name": "DMA_CHCTL",
                "offset": 8,
                "width": 32,
                "array_parameters": {"channel": {"start": 0, "stride": 20}},
            },
        ]
        dma = {
            "dma_channels": [0, 1, 2],
            "dmamux_channels": [0, 1, 2, 3],
            "dmamux_generator_channels": [0, 1],
        }

        result = MODULE.apply_dma_array_bounds(registers, dma)

        self.assertEqual(result[0]["array_parameters"]["channel"]["end"], 3)
        self.assertEqual(result[1]["array_parameters"]["channel"]["end"], 1)
        self.assertEqual(result[2]["array_parameters"]["channel"]["end"], 2)

    def test_解析当前分支的mdma请求与通道(self):
        macros = """\
#define MDMA_REQUEST_DMA0_CH0_FTFIF CHXCTL1_TRIGSEL(0)
#define MDMA_REQUEST_OSPI0_FT CHXCTL1_TRIGSEL(22U)
#define MDMA_REQUEST_SW ((uint32_t)0x40000000U)
"""
        source = """\
typedef enum {
    MDMA_CH0 = 0,
    MDMA_CH1,
} mdma_channel_enum;
"""

        self.assertEqual(
            MODULE.parse_mdma_facts(macros, source),
            {
                "channels": [0, 1],
                "requests": [
                    {
                        "kind": "hardware",
                        "name": "DMA0_CH0_FTFIF",
                        "request": 0,
                        "source_name": "MDMA_REQUEST_DMA0_CH0_FTFIF",
                    },
                    {
                        "kind": "hardware",
                        "name": "OSPI0_FT",
                        "request": 22,
                        "source_name": "MDMA_REQUEST_OSPI0_FT",
                    },
                    {
                        "kind": "software",
                        "name": "SW",
                        "request": 0x40000000,
                        "source_name": "MDMA_REQUEST_SW",
                    },
                ],
            },
        )

    def test_用函数参数的连续枚举补齐数组上界(self):
        registers = [
            {
                "name": "MDMA_CHXCTL",
                "offset": 0,
                "width": 32,
                "array_parameters": {"mdma_chx": {"start": 0, "stride": 64}},
            }
        ]
        source = """
typedef enum {
    MDMA_CH0 = 0,
    MDMA_CH1,
    MDMA_CH2,
} mdma_channel_enum;
void mdma_channel_enable(mdma_channel_enum mdma_chx);
"""

        result = MODULE.infer_typed_enum_bounds(registers, source)

        self.assertEqual(
            result[0]["array_parameters"]["mdma_chx"],
            {
                "start": 0,
                "end": 2,
                "stride": 64,
                "bound_evidence": "typed-enum:mdma_channel_enum",
            },
        )

    def test_枚举候选不连续或上界冲突时拒绝猜测(self):
        registers = [
            {
                "name": "TEST_CHXCTL",
                "offset": 0,
                "width": 32,
                "array_parameters": {"channel": {"start": 0, "stride": 4}},
            }
        ]
        source = """
typedef enum { TEST_CH0 = 0, TEST_CH2 = 2 } sparse_enum;
typedef enum { OTHER_CH0 = 0, OTHER_CH1 = 1 } short_enum;
typedef enum { OTHER_CH0X = 0, OTHER_CH1X = 1, OTHER_CH2X = 2 } long_enum;
void sparse(sparse_enum channel);
void short_use(short_enum channel);
void long_use(long_enum channel);
"""

        self.assertNotIn(
            "end",
            MODULE.infer_typed_enum_bounds(registers, source)[0][
                "array_parameters"
            ]["channel"],
        )

    def test_组合源码按外设块筛选同名枚举参数(self):
        registers = [
            {
                "name": "DMA_CHCTL",
                "offset": 0,
                "width": 32,
                "array_parameters": {"channel": {"start": 0, "stride": 4}},
            }
        ]
        source = """
typedef enum { DMA_CH0 = 0, DMA_CH1, DMA_CH2 } dma_channel_enum;
typedef enum { TIMER_CH0 = 0, TIMER_CH1 } timer_channel_enum;
void dma_use(dma_channel_enum channelx);
void timer_use(timer_channel_enum channel);
"""

        result = MODULE.infer_typed_enum_bounds(
            registers, source, {"DMA_CHCTL": "DMA"}
        )

        self.assertEqual(
            result[0]["array_parameters"]["channel"]["end"], 2
        )

    def test_数组宏占位名映射到同外设枚举参数(self):
        registers = [
            {
                "name": "HPDF_FLTYCTL",
                "offset": 0,
                "width": 32,
                "array_parameters": {"flty": {"start": 0, "stride": 128}},
            }
        ]

        result = MODULE.apply_typed_enum_bounds(
            registers,
            {"filtery": [("hpdf_filter_enum", 3)]},
            {"HPDF_FLTYCTL": "HPDF"},
        )

        self.assertEqual(result[0]["array_parameters"]["flty"]["end"], 3)

    def test_索引阶段的官方范围合并回当前激活寄存器(self):
        active = [
            {
                "name": "EXMC_NPCTL",
                "offset": 0x40,
                "width": 32,
                "array_parameters": {"bank": {"start": 0, "stride": 0x20}},
            }
        ]
        indexed = [
            {
                "name": "EXMC_NPCTL",
                "offset": 0x60,
                "width": 32,
                "array_parameters": {
                    "bank": {
                        "start": 1,
                        "end": 3,
                        "stride": 0x20,
                        "bound_evidence": "source-comment-range",
                    }
                },
            }
        ]

        result = MODULE.apply_source_array_bounds(active, indexed)

        self.assertEqual(result[0]["offset"], 0x60)
        self.assertEqual(result[0]["array_parameters"]["bank"]["end"], 3)

    def test_用外设专属官方编号组补齐数组范围(self):
        registers = [
            {
                "name": "SAI_CFG0",
                "offset": 4,
                "width": 32,
                "array_parameters": {"blocky": {"start": 0, "stride": 32}},
            }
        ]

        result = MODULE.apply_indexed_identifier_bounds(
            registers,
            {"SAI_BLOCK": [0, 1]},
            {"SAI_CFG0": "SAI"},
        )

        self.assertEqual(
            result[0]["array_parameters"]["blocky"],
            {
                "start": 0,
                "end": 1,
                "stride": 32,
                "bound_evidence": "indexed-identifiers:SAI_BLOCK",
            },
        )

    def test_EDIM_AFMT二维接收寄存器使用官方数据与从机编号组(self):
        registers = [
            {
                "name": "EDIM_AFMT_ENCRDATA",
                "offset": 0,
                "width": 32,
                "array_parameters": {
                    "m": {"start": 0, "stride": 4},
                    "n": {"start": 0, "stride": 16},
                },
            }
        ]

        result = MODULE.apply_indexed_identifier_bounds(
            registers,
            {"EDIM_AFMT_RDATA": [0, 1, 2], "EDIM_AFMT_SLAVE": list(range(8))},
            {"EDIM_AFMT_ENCRDATA": "EDIM_AFMT"},
        )

        self.assertEqual(result[0]["array_parameters"]["m"]["end"], 2)
        self.assertEqual(result[0]["array_parameters"]["n"]["end"], 7)

    def test_H77编号常量补齐_biss_tfmt_nvmc_数组范围(self):
        registers = [
            {
                "name": "EDIM_BISS_SnDATA0",
                "offset": 0,
                "width": 32,
                "array_parameters": {"n": {"start": 0, "stride": 8}},
            },
            {
                "name": "EDIM_TFMT_RDATA",
                "offset": 0x18,
                "width": 32,
                "array_parameters": {"x": {"start": 0, "stride": 4}},
            },
            {
                "name": "NVMC_CNVM_ROBBADDRX",
                "offset": 0xCC,
                "width": 32,
                "array_parameters": {"x": {"start": 0, "stride": 4}},
            },
        ]

        result = MODULE.apply_indexed_identifier_bounds(
            registers,
            {
                "EDIM_BISS_SLAVE": list(range(8)),
                "EDIM_TFMT_RDATA": list(range(4)),
                "NVMC_CNVM_ROBBADDR": list(range(4)),
            },
            {
                "EDIM_BISS_SnDATA0": "EDIM_BISS",
                "EDIM_TFMT_RDATA": "EDIM_TFMT",
                "NVMC_CNVM_ROBBADDRX": "NVMC",
            },
        )

        self.assertEqual(result[0]["array_parameters"]["n"]["end"], 7)
        self.assertEqual(result[1]["array_parameters"]["x"]["end"], 3)
        self.assertEqual(result[2]["array_parameters"]["x"]["end"], 3)

    def test_官方源文件循环证据补齐单参数数组范围(self):
        registers = [
            {
                "name": "EDIM_BISS_CCDATAx",
                "offset": 0x80,
                "width": 32,
                "array_parameters": {"n": {"start": 0, "stride": 4}},
            }
        ]

        result = MODULE.apply_source_loop_bounds(
            registers,
            [
                {
                    "register": "EDIM_BISS_CCDATAx",
                    "start": 0,
                    "end": 15,
                    "source": {"path": "Source/edim_biss.c", "sha256": "a" * 64},
                }
            ],
        )

        self.assertEqual(
            result[0]["array_parameters"]["n"],
            {
                "start": 0,
                "end": 15,
                "stride": 4,
                "bound_evidence": "source-loop:Source/edim_biss.c",
            },
        )

    def test_官方稀疏编号保留地址洞(self):
        registers = [
            {
                "name": "SYSCFG_TIMERCFG0",
                "offset": 0x100,
                "width": 32,
                "array_parameters": {"syscfg_timerx": {"start": 0, "stride": 12}},
            }
        ]

        result = MODULE.apply_indexed_identifier_bounds(
            registers,
            {"SYSCFG_TIMER": [0, 1, 2, 11]},
            {"SYSCFG_TIMERCFG0": "SYSCFG"},
        )

        self.assertEqual(
            result[0]["array_parameters"]["syscfg_timerx"],
            {
                "start": 0,
                "indices": [0, 1, 2, 11],
                "stride": 12,
                "bound_evidence": "indexed-identifiers:SYSCFG_TIMER",
            },
        )

    def test_按当前芯片选择TZBMPC实例数组范围(self):
        source = """
for TZBMPC0 and TZBMPC1 block position number is 0-255;
for TZBMPC2 block position number is 0-511 only for GD32W515;
for TZBMPC2 block position number is 0-255 only for GD32F5HC;
for TZBMPC3 block position number is 0-767 only for GD32W515;
for TZBMPC3 block position number is 0-511 only for GD32F5HC;
integer = block_pos_num / 32U;
"""
        source_info = {"path": "Firmware/Source/gd32_tzpcu.c", "sha256": "a" * 64}

        w515 = MODULE.parse_tzbmpc_instance_array_bounds(
            source, ["GD32W515"], source_info
        )
        f5hc = MODULE.parse_tzbmpc_instance_array_bounds(
            source, ["GD32F5HC"], source_info
        )

        self.assertEqual(
            {row["instance"]: row["end"] for row in w515},
            {"TZBMPC0": 7, "TZBMPC1": 7, "TZBMPC2": 15, "TZBMPC3": 23},
        )
        self.assertEqual(
            {row["instance"]: row["end"] for row in f5hc},
            {"TZBMPC0": 7, "TZBMPC1": 7, "TZBMPC2": 7, "TZBMPC3": 15},
        )
        self.assertTrue(all(row["source"] == source_info for row in w515))

    def test_用寄存器族专属编号组区分同名参数(self):
        registers = [
            {
                "name": "MFCOM_SCTL",
                "offset": 0x80,
                "width": 32,
                "array_parameters": {"x": {"start": 0, "stride": 4}},
            },
            {
                "name": "MFCOM_TMCTL",
                "offset": 0x400,
                "width": 32,
                "array_parameters": {"x": {"start": 0, "stride": 4}},
            },
        ]

        result = MODULE.apply_indexed_identifier_bounds(
            registers,
            {"MFCOM_SHIFTER_": [0, 1, 2, 3], "MFCOM_TIMER_": [0, 1]},
            {"MFCOM_SCTL": "MFCOM", "MFCOM_TMCTL": "MFCOM"},
        )

        self.assertEqual(result[0]["array_parameters"]["x"]["end"], 3)
        self.assertEqual(result[1]["array_parameters"]["x"]["end"], 1)

    def test_源码预处理模式保留当前设备中断枚举(self):
        compiler_name = shutil.which("clang")
        if compiler_name is None:
            self.skipTest("缺少 clang")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header = root / "device.h"
            header.write_text(
                "#if defined(DEVICE_A)\n"
                "typedef enum { TIMER0_IRQn = 25, } IRQn_Type;\n"
                "#else\n"
                "typedef enum { TIMER1_IRQn = 26, } IRQn_Type;\n"
                "#endif\n",
                encoding="utf-8",
            )
            sha256 = hashlib.sha256(header.read_bytes()).hexdigest()

            output = MODULE._preprocess(
                Path(compiler_name),
                "clang-test",
                [(header, sha256)],
                "a" * 64,
                ["DEVICE_A"],
                [],
                [root],
                root / "cache",
                mode="source",
            )

        self.assertEqual(
            MODULE.active_interrupts(output), [{"name": "TIMER0", "value": 25}]
        )

    def test_变体IR包含设备条件生效的中断(self):
        compiler_name = shutil.which("clang")
        if compiler_name is None:
            self.skipTest("缺少 clang")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device_path = root / "device.h"
            register_path = root / "periph.h"
            dma_path = root / "gd32test_dma.h"
            mdma_path = root / "gd32test_mdma.h"
            device_path.write_text(
                "#define PERIPH_BASE 0x40000000U\n"
                "#define PERIPH PERIPH_BASE\n"
                "#if defined(DEVICE_A)\n"
                "typedef enum { TIMER0_IRQn = 25, } IRQn_Type;\n"
                "#else\n"
                "typedef enum { TIMER1_IRQn = 26, } IRQn_Type;\n"
                "#endif\n",
                encoding="utf-8",
            )
            register_path.write_text(
                '#include "device.h"\n'
                "#define PERIPH_CTL REG32(PERIPH + 0x00U)\n"
                "#define PERIPH_CTL_EN BIT(0)\n"
                "#define PERIPH_CHCTL(channel) REG32(PERIPH + 0x20U + ((channel) * 4U))\n"
                "typedef enum { PERIPH_CH0 = 0, PERIPH_CH1 } periph_channel_enum;\n"
                "void periph_enable(periph_channel_enum channel);\n",
                encoding="utf-8",
            )
            dma_path.write_text(
                "#define RM_CHXCFG_MUXID(value) (value)\n"
                "#define DMA_REQUEST_ADC RM_CHXCFG_MUXID(5U)\n"
                "typedef enum {\n"
                "    DMA_CH0 = 0,\n"
                "} dma_channel_enum;\n"
                "typedef enum {\n"
                "    DMAMUX_MULTIPLEXER_CH0 = 0,\n"
                "} dmamux_multiplexer_channel_enum;\n",
                encoding="utf-8",
            )
            mdma_path.write_text(
                "#define CHXCTL1_TRIGSEL(value) (value)\n"
                "#define MDMA_REQUEST_PERIPH CHXCTL1_TRIGSEL(3U)\n"
                "#define MDMA_REQUEST_SW ((uint32_t)0x40000000U)\n"
                "typedef enum {\n"
                "    MDMA_CH0 = 0,\n"
                "} mdma_channel_enum;\n",
                encoding="utf-8",
            )
            device_sha = hashlib.sha256(device_path.read_bytes()).hexdigest()
            register_sha = hashlib.sha256(register_path.read_bytes()).hexdigest()
            dma_sha = hashlib.sha256(dma_path.read_bytes()).hexdigest()
            mdma_sha = hashlib.sha256(mdma_path.read_bytes()).hexdigest()
            library = {
                "series": "GD32TEST",
                "archive_sha256": "a" * 64,
                "tree_sha256": "b" * 64,
                "device_header_sha256": device_sha,
                "register_headers": [
                    {
                        "path": "periph.h",
                        "sha256": register_sha,
                        "license": "Apache-2.0",
                    },
                    {
                        "path": "gd32test_dma.h",
                        "sha256": dma_sha,
                        "license": "Apache-2.0",
                    },
                    {
                        "path": "gd32test_mdma.h",
                        "sha256": mdma_sha,
                        "license": "Apache-2.0",
                    },
                ],
            }

            result = MODULE._variant_ir(
                {
                    "id": "gd32test-a",
                    "series": "GD32TEST",
                    "defines": ["DEVICE_A"],
                    "devices": ["GD32TESTA"],
                },
                library,
                root,
                Path(compiler_name),
                "clang-test",
                {},
                root / "cache",
                device_header={"path": "device.h", "sha256": device_sha},
            )

        self.assertEqual(result["interrupts"], [{"name": "TIMER0", "value": 25}])
        self.assertEqual(result["base_addresses"], {"PERIPH_BASE": 0x40000000})
        array_register = next(
            register
            for layout in result["layouts"]
            for register in layout["registers"]
            if register["name"] == "PERIPH_CHCTL"
        )
        self.assertEqual(
            array_register["array_parameters"]["channel"],
            {
                "start": 0,
                "end": 1,
                "stride": 4,
                "bound_evidence": "typed-enum:periph_channel_enum",
            },
        )
        self.assertEqual(
            result["dma"],
            {
                "source": {"path": "gd32test_dma.h", "sha256": dma_sha},
                "kind": "dmamux",
                "dma_channels": [0],
                "dmamux_channels": [0],
                "dmamux_generator_channels": [],
                "requests": [
                    {
                        "name": "ADC",
                        "request": 5,
                        "source_name": "DMA_REQUEST_ADC",
                    }
                ],
            },
        )
        self.assertEqual(
            result["mdma"],
            {
                "source": {"path": "gd32test_mdma.h", "sha256": mdma_sha},
                "channels": [0],
                "requests": [
                    {
                        "kind": "hardware",
                        "name": "PERIPH",
                        "request": 3,
                        "source_name": "MDMA_REQUEST_PERIPH",
                    },
                    {
                        "kind": "software",
                        "name": "SW",
                        "request": 0x40000000,
                        "source_name": "MDMA_REQUEST_SW",
                    },
                ],
            },
        )

    def test_PDSC_define相同的设备合并为一个选择器(self):
        resources = {
            "devices": [
                {
                    "device": "GD32F103C8",
                    "source_pack_name": "GD32F10x_DFP",
                    "compile": [{"define": "GD32F10X_MD USE_STDPERIPH_DRIVER"}],
                },
                {
                    "device": "GD32F103CB",
                    "source_pack_name": "GD32F10x_DFP",
                    "compile": [{"define": "GD32F10X_MD USE_STDPERIPH_DRIVER"}],
                },
            ]
        }

        variants, missing = MODULE.collect_variants(resources, {"GD32F10x"})

        self.assertEqual(missing, [])
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["devices"], ["GD32F103C8", "GD32F103CB"])
        self.assertEqual(
            variants[0]["defines"], ["GD32F10X_MD", "USE_STDPERIPH_DRIVER"]
        )

    def test_源码中的设备名前缀补足PDSC泛化或缺失define(self):
        resources = {
            "devices": [
                {
                    "device": "GD32G553CB",
                    "source_pack_name": "GD32G5x3_DFP",
                    "compile": [{"define": "GD32G5x3"}],
                },
                {
                    "device": "GD32G533CB",
                    "source_pack_name": "GD32G5x3_DFP",
                    "compile": [],
                },
            ]
        }

        variants, missing = MODULE.collect_variants(
            resources,
            {"GD32G5x3"},
            {"GD32G5x3": ["GD32G533", "GD32G553"]},
        )

        self.assertEqual(missing, [])
        self.assertEqual(
            [variant["defines"] for variant in variants],
            [["GD32G533"], ["GD32G553", "GD32G5x3"]],
        )

    def test_PDSC无define时由Firmware系列宏形成变体(self):
        resources = {
            "devices": [
                {
                    "device": "GD32A503CB",
                    "source_pack_name": "GD32A50x_DFP",
                    "compile": [],
                }
            ]
        }

        variants, missing = MODULE.collect_variants(resources, {"GD32A50x"})

        self.assertEqual(missing, [])
        self.assertEqual(variants[0]["series"], "GD32A50x")
        self.assertEqual(variants[0]["defines"], [])

    def test_Pack到Firmware显式别名不能跨出器件范围(self):
        resources = {
            "devices": [
                {
                    "device": "GD32H767IM",
                    "source_pack_name": "GD32H7xx_DFP",
                    "compile": [{"define": "GD32H7XXI"}],
                }
            ]
        }

        variants, missing = MODULE.collect_variants(
            resources, {"GD32H73x_75x"}
        )

        self.assertEqual(variants, [])
        self.assertEqual(
            missing,
            [{"device": "GD32H767IM", "reason": "firmware-series-not-matched"}],
        )

    def test_无CMSIS规范型号也进入Firmware变体或明确缺口(self):
        models = {
            "devices": [
                {
                    "id": "GD32VF103C8",
                    "cmsis_devices": [],
                    "source_packs": [],
                },
                {
                    "id": "GD32A711AR",
                    "cmsis_devices": [],
                    "source_packs": [],
                },
            ]
        }

        variants, missing = MODULE.collect_variants(
            {"devices": []},
            {"GD32VF103"},
            {"GD32VF103": ["GD32VF103"]},
            models,
        )

        self.assertEqual(variants[0]["devices"], ["GD32VF103C8"])
        self.assertEqual(variants[0]["defines"], ["GD32VF103"])
        self.assertEqual(
            missing,
            [{"device": "GD32A711AR", "reason": "firmware-series-not-matched"}],
        )

    def test_Builder型号与固件宏顺序不同时采用已验证显式映射(self):
        models = {
            "devices": [
                {
                    "id": "GD32H77DIX",
                    "cmsis_devices": [],
                    "source_packs": [],
                }
            ]
        }

        variants, missing = MODULE.collect_variants(
            {"devices": []},
            {"GD32H77X_78X"},
            {"GD32H77X_78X": ["GD32H77DXI"]},
            models,
            {"GD32H77DIX": ["GD32H77DXI"]},
        )

        self.assertEqual(missing, [])
        self.assertEqual(variants[0]["defines"], ["GD32H77DXI"])

    def test_Builder选择器X自动匹配Programmer精确型号(self):
        models = {
            "devices": [
                {
                    "id": device,
                    "cmsis_devices": [],
                    "source_packs": [],
                }
                for device in [
                    "GD32H779VP",
                    "GD32H77DII",
                    "GD32H77EIW",
                    "GD32H77RAP",
                ]
            ]
        }

        variants, missing = MODULE.collect_variants(
            {"devices": []},
            {"GD32H77X_78X"},
            {
                "GD32H77X_78X": [
                    "GD32H779XP",
                    "GD32H77DXI",
                    "GD32H77EXW",
                    "GD32H77RXP",
                    "GD32H77X",
                ]
            },
            models,
        )

        self.assertEqual(missing, [])
        self.assertEqual(
            {tuple(variant["defines"]): variant["devices"] for variant in variants},
            {
                ("GD32H779XP",): ["GD32H779VP"],
                ("GD32H77DXI",): ["GD32H77DII"],
                ("GD32H77EXW",): ["GD32H77EIW"],
                ("GD32H77RXP",): ["GD32H77RAP"],
            },
        )

    def test_CMSIS车规别名只生成一个规范设备变体(self):
        resources = {
            "devices": [
                {
                    "device": alias,
                    "source_pack_name": "GD32A50x_DFP",
                    "compile": [{"define": "GD32A503"}],
                }
                for alias in ["GD32A503CB", "GD32A503CBT30E"]
            ]
        }
        models = {
            "devices": [
                {
                    "id": "GD32A503CB",
                    "cmsis_devices": ["GD32A503CB", "GD32A503CBT30E"],
                    "source_packs": [{"name": "GD32A50x_DFP"}],
                }
            ]
        }

        variants, missing = MODULE.collect_variants(
            resources,
            {"GD32A50X"},
            {},
            models,
        )

        self.assertEqual(missing, [])
        self.assertEqual(variants[0]["devices"], ["GD32A503CB"])

    def test_Builder显式映射同时校验系列选择器和头文件哈希(self):
        config = {
            "mappings": [
                {
                    "device": "GD32H77DIX",
                    "series": "GD32H77X_78X",
                    "selector": "GD32H77DXI",
                    "device_header_sha256": "a" * 64,
                    "matrix_paths": ["h77.xml"],
                }
            ]
        }
        models = {"devices": [{"id": "GD32H77DIX", "source_packs": []}]}

        result = MODULE.validated_device_selectors(
            config,
            models,
            {"GD32H77X_78X"},
            {"GD32H77X_78X": ["GD32H77DXI"]},
            {"GD32H77X_78X": {"sha256": "a" * 64}},
        )

        self.assertEqual(result, {"GD32H77DIX": ["GD32H77DXI"]})

    def test_预处理内部规范GD32选择器大小写并补系列宏(self):
        self.assertEqual(
            MODULE.preprocessor_defines(
                {"series": "GD32E50x", "defines": ["GD32E50x_HD"]},
                [],
                {
                    "gd32e50x": ["GD32E50X"],
                    "gd32e50x_hd": ["GD32E50X_HD"],
                },
            ),
            ["GD32E50X", "GD32E50X_HD", "HXTAL_VALUE=0U"],
        )
        self.assertEqual(
            MODULE.preprocessor_defines(
                {"series": "GD32E51x", "defines": ["GD32EPRTxxA"]},
                [],
                {
                    "gd32e51x": ["GD32E51X"],
                    "gd32eprtxxa": ["GD32EPRTxxA"],
                },
            ),
            ["GD32E51X", "GD32EPRTxxA", "HXTAL_VALUE=0U"],
        )

    def test_已知Firmware问题必须锁定选择器头文件哈希和精确差异(self):
        issue = {
            "series": "GD32L23x",
            "defines": ["GD32L235", "GD32L23x"],
            "tree_sha256": "c" * 64,
            "path": "gd32l23x_rcu.h",
            "sha256": "a" * 64,
            "invalid_fields": [
                {"name": "RCU_CFG2_LPUART1SEL", "first": 25, "second": 24}
            ],
        }
        expected = {
            **issue,
            "resolution": "block",
            "reason": "官方 Firmware 位域范围反向",
        }

        self.assertEqual(
            MODULE.classify_source_issue(issue, expected), "known-blocking"
        )
        self.assertEqual(
            MODULE.classify_source_issue(
                issue, {**expected, "resolution": "prefer-pack-svd"}
            ),
            "source-resolved",
        )
        self.assertEqual(
            MODULE.classify_source_issue(
                {**issue, "sha256": "b" * 64}, expected
            ),
            "unexpected",
        )


if __name__ == "__main__":
    unittest.main()
