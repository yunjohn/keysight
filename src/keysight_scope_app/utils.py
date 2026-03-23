from __future__ import annotations

import math


INVALID_MEASUREMENT_THRESHOLD = 9e36
SI_PREFIXES = {
    -12: "p",
    -9: "n",
    -6: "u",
    -3: "m",
    0: "",
    3: "k",
    6: "M",
    9: "G",
}


def is_invalid_measurement(value: float) -> bool:
    return math.isnan(value) or math.isinf(value) or abs(value) >= INVALID_MEASUREMENT_THRESHOLD


def format_engineering_value(value: float, unit: str) -> str:
    if is_invalid_measurement(value):
        return "无效"
    if value == 0:
        return f"0 {unit}".strip()

    exponent = int(math.floor(math.log10(abs(value)) / 3) * 3)
    exponent = min(max(exponent, min(SI_PREFIXES)), max(SI_PREFIXES))
    scaled = value / (10 ** exponent)
    prefix = SI_PREFIXES[exponent]
    return f"{scaled:.4g} {prefix}{unit}".strip()


def strip_ieee4882_block(data: bytes) -> bytes:
    if not data:
        raise ValueError("仪器未返回任何二进制数据。")
    if data[:1] != b"#":
        return data

    digits_count = int(data[1:2].decode("ascii"))
    if digits_count <= 0:
        raise ValueError("无效的 IEEE 488.2 数据块头。")

    header_end = 2 + digits_count
    payload_length = int(data[2:header_end].decode("ascii"))
    payload_end = header_end + payload_length
    return data[header_end:payload_end]

