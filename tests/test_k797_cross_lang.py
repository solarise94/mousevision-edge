"""跨语言契约一致性守护测试（K797 解析器：Python ↔ ArkTS）。

计划 §9.1 / §14 要求 Python、ArkTS、Kotlin 三端解析器共用同一组 fixture 与
JSON 契约，且"双端实现逐渐出现协议差异"被列为风险。本测试不依赖设备/编译，
直接解析 ArkTS 源码文本，断言其协议常量、deviceKey 大小写、校验顺序与
Python 实现（``mousevision.scale_k797``）及计划 §5.1 契约示例逐字一致。

它曾抓到一个真实 bug：ArkTS ``buildDeviceKey`` 误用 toUpperCase 生成大写
deviceKey，而 Python ``_DEVICE_KEY`` 与契约示例均为小写
（k797:0000:cae803280895ca0210）。本测试将该 bug 固化为回归守护。

ArkTS 侧的 on-device hypium instrumentation 测试因需完整 ohosTest 脚手架
且运行依赖设备 instrumentation，留待设备可用时搭建；本测试作为零设备依赖的
CI 守护，保证两端常量/契约不漂移。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mousevision import scale_k797 as pyk

REPO = Path(__file__).resolve().parents[1]
ARKTS_PARSER = (
    REPO
    / "harmonyos"
    / "MiceAutomaticScale"
    / "entry"
    / "src"
    / "main"
    / "ets"
    / "scale"
    / "K797AdvertisementParser.ets"
)

# 计划 §5.1 契约示例中的 deviceKey（小写 hex）。
CONTRACT_DEVICE_KEY = "k797:0000:cae803280895ca0210"


@pytest.fixture(scope="module")
def arkts_src() -> str:
    return ARKTS_PARSER.read_text(encoding="utf-8")


def _arkts_const(src: str, name: str) -> str:
    """提取 `export const NAME: <type> = <value>;` 的 value 文本。"""
    m = re.search(rf"export\s+const\s+{name}\s*:\s*[^=]+=\s*([^;]+);", src)
    assert m, f"ArkTS const {name} not found"
    return m.group(1).strip()


def _arkts_prefix_bytes(src: str) -> bytes:
    arr = _arkts_const(src, "K797_PREFIX")
    nums = re.findall(r"0x([0-9A-Fa-f]{1,2})", arr)
    return bytes(int(n, 16) for n in nums)


# --------------------------------------------------------------------- #
# 常量逐字一致
# --------------------------------------------------------------------- #


def test_prefix_bytes_match(arkts_src: str) -> None:
    assert _arkts_prefix_bytes(arkts_src) == pyk.K797_PREFIX


def test_local_name_matches(arkts_src: str) -> None:
    assert _arkts_const(arkts_src, "K797_DEVICE_NAME").strip("'\"") == pyk.K797_LOCAL_NAME


def test_manufacturer_id_matches(arkts_src: str) -> None:
    assert int(_arkts_const(arkts_src, "K797_MANUFACTURER_ID"), 16) == pyk.K797_MANUFACTURER_ID


def test_min_payload_len_matches(arkts_src: str) -> None:
    assert int(_arkts_const(arkts_src, "K797_MIN_PAYLOAD_BYTES")) == pyk.K797_MIN_PAYLOAD_LEN


def test_max_grams_matches(arkts_src: str) -> None:
    assert float(_arkts_const(arkts_src, "K797_MAX_GRAMS")) == pytest.approx(pyk.K797_MAX_GRAMS)


# --------------------------------------------------------------------- #
# deviceKey 大小写一致（回归守护：曾误用大写）
# --------------------------------------------------------------------- #


def test_python_device_key_is_contract_lowercase() -> None:
    assert pyk._DEVICE_KEY == CONTRACT_DEVICE_KEY


def test_arkts_build_device_key_uses_lowercase(arkts_src: str) -> None:
    """buildDeviceKey 必须用 toLowerCase，否则 deviceKey 与契约/Python 漂移。"""
    m = re.search(
        r"function\s+buildDeviceKey\([^)]*\)\s*:\s*string\s*\{(.*?)\n\}",
        arkts_src,
        re.S,
    )
    assert m, "buildDeviceKey body not found"
    body = m.group(1)
    assert "toLowerCase" in body, "buildDeviceKey must use toLowerCase for contract parity"
    assert "toUpperCase" not in body, "buildDeviceKey must NOT use toUpperCase (drifts from contract)"


def test_arkts_payload_hex_still_uppercase(arkts_src: str) -> None:
    """payloadHex 显示用大写（契约 §5.1 payloadHex 为大写），不得误改成小写。"""
    m = re.search(r"function\s+toHexSpaces\([^)]*\)\s*:\s*string\s*\{(.*?)\n\}", arkts_src, re.S)
    assert m, "toHexSpaces body not found"
    assert "toUpperCase" in m.group(1), "toHexSpaces must keep upper-case hex for payloadHex"


def test_simulated_arkts_device_key_equals_contract() -> None:
    """用 Python 模拟 ArkTS buildDeviceKey 的小写逻辑，结果应等于契约 deviceKey。"""
    id_hex = f"{pyk.K797_MANUFACTURER_ID:04x}"  # 小写 4 位
    prefix_hex = "".join(f"{b:02x}" for b in pyk.K797_PREFIX)  # 小写
    assert f"k797:{id_hex}:{prefix_hex}" == CONTRACT_DEVICE_KEY


# --------------------------------------------------------------------- #
# 校验顺序一致（name → id → length → prefix → weight）
# --------------------------------------------------------------------- #


def test_validation_order_matches_python(arkts_src: str) -> None:
    """ArkTS parse() 的拒绝顺序必须与 Python 一致，否则同包可能一端通过一端拒绝。"""
    # 取 parse 函数体（从 export function parse 到其配对的结束不易精确，
    # 用各校验标志行的出现位置做相对顺序断言即可）。
    markers = [
        r"localName\s*!==\s*K797_DEVICE_NAME",          # wrong_name
        r"manufacturerId\s*!==\s*K797_MANUFACTURER_ID",  # wrong_manufacturer_id
        r"payload\.length\s*<\s*K797_MIN_PAYLOAD_BYTES",  # payload_too_short
        r"prefixMatch\s*!==\s*0",                        # wrong_prefix
        r"grams\s*>\s*K797_MAX_GRAMS",                   # weight_out_of_protocol_range
    ]
    positions = []
    for pat in markers:
        m = re.search(pat, arkts_src)
        assert m, f"validation marker not found: {pat}"
        positions.append(m.start())
    assert positions == sorted(positions), (
        f"ArkTS validation order drifts from contract: positions={positions}"
    )


def test_python_reject_reason_set_matches_contract() -> None:
    """Python 拒绝原因集合应与计划 §6 枚举一致（间接保证 ArkTS 同枚举）。"""
    expected = {
        "wrong_name",
        "wrong_manufacturer_id",
        "payload_too_short",
        "wrong_prefix",
        "weight_out_of_protocol_range",
    }
    assert {r.value for r in pyk.ParseRejectReason} == expected
