import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "extract_gigadevice_firmware_registers.py"
SPEC = importlib.util.spec_from_file_location(
    "extract_gigadevice_firmware_registers", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


HEADER = """\
#define USART1 USART_BASE
#define USART0 (USART_BASE + 0xF400U)
#define SDIO (APB2_BUS_BASE + 0x2C00U)
#define USART_STAT(usartx) REG32((usartx) + 0x00U)
#define USART_DATA(usartx) REG16((usartx) + 0x04U)
#define USART_GP(usartx) REG32((usartx) + 0x1CU)
#define DMA_CHCTL(channel) REG32((DMA + 0x08U) + 0x14U * (channel))
#define DMA_CHANNEL(dmax, channel) ((dmax) + 0x14U * (channel))
#define DMA_CHMADDR(dmax, channel) REG32(DMA_CHANNEL((dmax), (channel)) + 0x0CU)
#define SDIO_PWRCTL REG32(SDIO + 0x00U)
#define SPI_REG_VAL(offset) REG32(spi_periph + (offset))
#define USART_STAT_PERR BIT(0)
#define USART_DATA_DATA BITS(0,8)
#define USART_DATA_BROKEN BITS(8,7)
"""


class FirmwareRegisterTests(unittest.TestCase):
    def test_数组上界枚举只索引函数参数不误收返回函数名(self):
        source = """
typedef enum { HPDF_FILTER0 = 0, HPDF_FILTER1 } hpdf_filter_enum;
hpdf_filter_enum hpdf_filter_get(uint32_t hpdf, hpdf_filter_enum filtery);
"""

        self.assertEqual(
            MODULE.typed_enum_candidates(source),
            {"filtery": [("hpdf_filter_enum", 1)]},
        )

    def test_索引官方常量和枚举成员的编号组(self):
        source = """
#define SAI_BLOCK0 0U
#define SAI_BLOCK1 1U
typedef enum { MASTER_PORT0 = 0, MASTER_PORT1, MASTER_PORT2 } master_port_enum;
#define EXMC_BANK0_NORSRAM_REGION0 0U
#define SINGLE0 0U
"""

        self.assertEqual(
            MODULE.indexed_identifier_groups(source),
            {
                "EXMC_BANK0_NORSRAM_REGION": [0],
                "MASTER_PORT": [0, 1, 2],
                "SAI_BLOCK": [0, 1],
            },
        )

    def test_索引官方编号常量的实际连续值(self):
        source = """
#define SYSCFG_TIMER0 ((uint8_t)0x00U)
#define SYSCFG_TIMER2 ((uint8_t)0x01U)
#define SYSCFG_TIMER7 ((uint8_t)0x02U)
#define SYSCFG_TIMER40 ((uint8_t)0x0BU)
"""

        self.assertEqual(
            MODULE.indexed_identifier_value_groups(source),
            {"SYSCFG_TIMER": [0, 1, 2, 11]},
        )

    def test_位域前缀命中窄别名但范围越界时回退到宽寄存器(self):
        source = """\
#define POC 0x40010000U
#define POC_ODMODE0 REG32(POC + 0x40U)
#define POC_ODMODE0_TIMER0 REG8(POC + 0x40U)
#define POC_ODMODE0_TIMER7 REG8(POC + 0x41U)
#define POC_ODMODE0_TIMER0_OSEL0 BITS(0,1)
#define POC_ODMODE0_TIMER7_OSEL0 BITS(8,9)
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )

        self.assertEqual(
            facts["fields"],
            [
                {
                    "bit_offset": 0,
                    "bit_size": 2,
                    "name": "POC_ODMODE0_TIMER0_OSEL0",
                    "register": "POC_ODMODE0_TIMER0",
                },
                {
                    "bit_offset": 8,
                    "bit_size": 2,
                    "name": "POC_ODMODE0_TIMER7_OSEL0",
                    "register": "POC_ODMODE0",
                },
            ],
        )

    def test_厂商单参数BITS记录为非法来源而不崩溃(self):
        source = """\
#define PMU 0x40007000U
#define PMU_CTL4 REG32(PMU + 0x10U)
#define PMU_CTL4_FUVDEN BITS(22)
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )

        self.assertEqual(
            facts["invalid_fields"],
            [
                {
                    "first": 22,
                    "name": "PMU_CTL4_FUVDEN",
                    "reason": "BITS缺少结束位",
                    "second": None,
                }
            ],
        )

    def test_区分基址参数与数组参数并恢复宏名范围(self):
        source = """\
#define BKP 0x40006C00U
#define BKP_DATA10_41(number) REG16(BKP + 0x40U + ((number) - 10U) * 0x04U)
#define USART_STAT(usartx) REG32((usartx) + 0x00U)
#define DMA_CHCTL(dmax, channel) REG32((dmax) + 0x08U + 0x14U * (channel))
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )
        registers = {row["name"]: row for row in facts["registers"]}

        self.assertEqual(registers["USART_STAT"]["base_parameters"], ["usartx"])
        self.assertNotIn("array_parameters", registers["USART_STAT"])
        self.assertEqual(registers["DMA_CHCTL"]["base_parameters"], ["dmax"])
        self.assertEqual(
            registers["DMA_CHCTL"]["array_parameters"],
            {"channel": {"start": 0, "stride": 20}},
        )
        self.assertEqual(registers["BKP_DATA10_41"]["offset"], 0x40)
        self.assertEqual(
            registers["BKP_DATA10_41"]["array_parameters"],
            {"number": {"start": 10, "end": 41, "stride": 4}},
        )

    def test_从寄存器宏同行官方注释提取参数范围(self):
        source = """
#define EXMC 0x40000000U
#define EXMC_NPCTL(bank) REG32(EXMC + 0x40U + 0x20U * (bank)) /*!< bank = 1,2,3 */
#define EXMC_SDCTL(device) REG32(EXMC + 0x140U + 0x4U * ((device) - 4U)) /*!< device = 0..1 */
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )
        registers = {row["name"]: row for row in facts["registers"]}

        self.assertEqual(
            registers["EXMC_NPCTL"]["array_parameters"]["bank"],
            {
                "start": 1,
                "end": 3,
                "stride": 32,
                "bound_evidence": "source-comment-range",
            },
        )
        self.assertEqual(registers["EXMC_NPCTL"]["offset"], 0x60)
        self.assertEqual(
            registers["EXMC_SDCTL"]["array_parameters"]["device"]["end"], 1
        )

    def test_从寄存器分组注释提取占位参数范围(self):
        source = """
#define SHRTIMER 0x40000000U
/* Slave_TIMERx(x=0..4) registers definitions */
#define SHRTIMER_STXCTL(shrtimery, slavex) REG32((shrtimery) + ((slavex) + 1U) * 0x80U)
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )

        self.assertEqual(
            facts["registers"][0]["array_parameters"]["slavex"],
            {
                "start": 0,
                "end": 4,
                "stride": 128,
                "bound_evidence": "source-comment-range",
            },
        )

    def test_寄存器分组范围不会泄漏到下一章节(self):
        source = """
#define UNIT 0x40000000U
/* TIMERx(x=0..4) registers definitions */
#define UNIT_TIMER(x) REG32(UNIT + (x) * 4U)
/* common registers definitions */
#define UNIT_COMMON(x) REG32(UNIT + 0x100U + (x) * 4U)
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )
        registers = {row["name"]: row for row in facts["registers"]}

        self.assertEqual(registers["UNIT_TIMER"]["array_parameters"]["x"]["end"], 4)
        self.assertNotIn("end", registers["UNIT_COMMON"]["array_parameters"]["x"])

    def test_从同行官方注释提取连字符和上界范围(self):
        source = """
#define UNIT 0x40000000U
#define UNIT_REGION(x) REG32(UNIT + 4U * (x)) /*!< region x(x = 0-3) */
#define UNIT_CTX(regval) REG32(UNIT + 0x20U + 4U * (regval)) /*!< regval<=53 */
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )
        registers = {row["name"]: row for row in facts["registers"]}

        self.assertEqual(registers["UNIT_REGION"]["array_parameters"]["x"]["end"], 3)
        self.assertEqual(
            registers["UNIT_CTX"]["array_parameters"]["regval"]["end"], 53
        )

    def test_从同行官方注释提取三点号和_to_范围(self):
        source = """
#define UNIT 0x40000000U
#define UNIT_PORT(x) REG32(UNIT + 4U * (x)) /*!< port x, x = 0...3 */
#define UNIT_STATUS(n) REG8(UNIT + 0x20U + (n)) /*!< status n (n = 0 to 7) */
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )
        registers = {row["name"]: row for row in facts["registers"]}

        self.assertEqual(registers["UNIT_PORT"]["array_parameters"]["x"]["end"], 3)
        self.assertEqual(registers["UNIT_STATUS"]["array_parameters"]["n"]["end"], 7)

    def test_从官方源文件固定上界循环提取寄存器数组范围(self):
        source = """
void clear(void)
{
    uint32_t reg_cnt;
    for(reg_cnt = 0U; reg_cnt < 16U; reg_cnt++) {
        EDIM_BISS_CCDATAx(reg_cnt) = 0U;
    }
}
"""

        self.assertEqual(
            MODULE.source_loop_array_bounds(source),
            [
                {
                    "end": 15,
                    "loop_variable": "reg_cnt",
                    "register": "EDIM_BISS_CCDATAx",
                    "start": 0,
                }
            ],
        )

    def test_从同头文件结构体数组恢复寄存器数组范围(self):
        source = """
#define CAU 0x40000000U
#define CAU_GCMCCMCTXSx(x) REG32(CAU + 0x50U + 4U * (x))
typedef struct {
    uint32_t gcmccmctxs[8];
} cau_context_parameter_struct;
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )

        self.assertEqual(
            facts["registers"][0]["array_parameters"]["x"],
            {
                "start": 0,
                "end": 7,
                "stride": 4,
                "bound_evidence": "source-struct-array",
            },
        )

    def test_从官方寄存器计数常量恢复数组范围(self):
        source = """
#define OB 0x1FFFF800U
#define OB_WORD_CNT 6U
#define OP_BYTE(x) REG32(OB + 4U * (x))
"""

        facts = MODULE.parse_register_facts(
            source, MODULE.resolve_integer_definitions(source)
        )

        self.assertEqual(
            facts["registers"][0]["array_parameters"]["x"]["end"], 5
        )

    def test_跨头文件地址宏在库级闭包中解析(self):
        values = MODULE.resolve_library_integer_definitions(
            [
                "#define SDIO0 (SDIO_BASE + 0x1000U)\n#define SDIO1 SDIO_BASE\n",
                "#define CPDM_SDIO0 (SDIO0 + 0x1000U)\n#define CPDM_SDIO1 (SDIO1 + 0x400U)\n",
            ],
            {"SDIO_BASE": 0x48000000},
        )

        self.assertEqual(values["CPDM_SDIO0"], 0x48002000)
        self.assertEqual(values["CPDM_SDIO1"], 0x48000400)

    def test_固定基址_owner_优先决定外设块(self):
        blocks = MODULE._peripheral_blocks(
            {"CMP": 0x40010000, "FMC": 0x40020000, "OB": 0x1FFF0000},
            [
                {"name": "CMP1_CS", "owner": "CMP"},
                {"name": "EFUSE_CTL", "owner": "FMC"},
                {"name": "OP_BYTE", "owner": "OB"},
            ],
        )

        self.assertEqual(
            blocks["register_blocks"],
            {"CMP1_CS": "CMP", "EFUSE_CTL": "FMC", "OP_BYTE": "OB"},
        )
        self.assertEqual(blocks["unassigned_instances"], [])
        self.assertEqual(blocks["unassigned_registers"], [])

    def test_未匹配实例归入包含其他变体的通用参数块(self):
        blocks = MODULE._peripheral_blocks(
            {"UART3": 0x40004C00, "USART0": 0x40013800, "USART5": 0x40017000},
            [
                {"name": "USART_STAT", "parameters": ["usartx"]},
                {"name": "USART5_STAT", "parameters": ["usartx"]},
            ],
        )

        self.assertEqual(blocks["instance_blocks"]["UART3"], "USART")
        self.assertEqual(blocks["unassigned_instances"], [])

    def test_解析实例寄存器偏移数组步长和字段(self):
        values = MODULE.resolve_integer_definitions(
            HEADER,
            {
                "USART_BASE": 0x40004400,
                "APB2_BUS_BASE": 0x40010000,
                "DMA": 0x40020000,
            },
        )

        facts = MODULE.parse_register_facts(HEADER, values)

        self.assertEqual(values["USART0"], 0x40013800)
        self.assertEqual(
            facts["instances"],
            {
                "DMA": 0x40020000,
                "SDIO": 0x40012C00,
                "USART0": 0x40013800,
                "USART1": 0x40004400,
            },
        )
        self.assertEqual(
            facts["instance_blocks"],
            {
                "DMA": "DMA",
                "SDIO": "SDIO",
                "USART0": "USART",
                "USART1": "USART",
            },
        )
        self.assertEqual(
            facts["register_blocks"],
            {
                "DMA_CHCTL": "DMA",
                "DMA_CHMADDR": "DMA",
                "SDIO_PWRCTL": "SDIO",
                "USART_DATA": "USART",
                "USART_GP": "USART",
                "USART_STAT": "USART",
            },
        )
        self.assertEqual(facts["unassigned_instances"], [])
        self.assertEqual(facts["unassigned_registers"], [])
        self.assertEqual(
            facts["registers"],
            [
                {
                    "array_parameters": {"channel": {"start": 0, "stride": 20}},
                    "name": "DMA_CHCTL",
                    "offset": 8,
                    "owner": "DMA",
                    "parameters": ["channel"],
                    "stride": 20,
                    "width": 32,
                },
                {
                    "array_parameters": {"channel": {"start": 0, "stride": 20}},
                    "base_parameters": ["dmax"],
                    "name": "DMA_CHMADDR",
                    "offset": 12,
                    "parameters": ["dmax", "channel"],
                    "stride": 20,
                    "width": 32,
                },
                {
                    "name": "SDIO_PWRCTL",
                    "offset": 0,
                    "owner": "SDIO",
                    "parameters": [],
                    "width": 32,
                },
                {
                    "base_parameters": ["usartx"],
                    "name": "USART_DATA",
                    "offset": 4,
                    "parameters": ["usartx"],
                    "width": 16,
                },
                {
                    "base_parameters": ["usartx"],
                    "name": "USART_GP",
                    "offset": 28,
                    "parameters": ["usartx"],
                    "width": 32,
                },
                {
                    "base_parameters": ["usartx"],
                    "name": "USART_STAT",
                    "offset": 0,
                    "parameters": ["usartx"],
                    "width": 32,
                },
            ],
        )
        self.assertEqual(
            facts["fields"],
            [
                {"bit_offset": 0, "bit_size": 9, "name": "USART_DATA_DATA", "register": "USART_DATA"},
                {"bit_offset": 0, "bit_size": 1, "name": "USART_STAT_PERR", "register": "USART_STAT"},
            ],
        )
        self.assertEqual(facts["unresolved_registers"], [])
        self.assertEqual(facts["helper_register_macros"], ["SPI_REG_VAL"])
        self.assertEqual(
            facts["invalid_fields"],
            [{"first": 8, "name": "USART_DATA_BROKEN", "second": 7}],
        )


if __name__ == "__main__":
    unittest.main()
