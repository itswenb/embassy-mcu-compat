#!/usr/bin/env python3
"""锁定 GigaDevice 官方产品选择器中的 GD32A7 公开事实。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

import gigadevice_sources as common


BASE_URL = "https://www.gigadevice.com"
SOURCE_PAGE = f"{BASE_URL}/product/mcu/automotive-mcus/gd32a7xx-series"


class _ProductTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, str]]] = []
        self._row: list[dict[str, str]] | None = None
        self._cell_tag: str | None = None
        self._text: list[str] = []
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell_tag = tag
            self._text = []
            self._href = ""
        elif tag == "a" and self._cell_tag is not None:
            self._href = dict(attrs).get("href") or ""

    def handle_data(self, data: str) -> None:
        if self._cell_tag is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._cell_tag and self._row is not None:
            self._row.append(
                {"text": " ".join("".join(self._text).split()), "href": self._href}
            )
            self._cell_tag = None
            self._text = []
            self._href = ""
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _header_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    for key in (
        "partno",
        "core",
        "series",
        "package",
        "maxspeedmhz",
        "flashbytes",
        "srambytes",
    ):
        if normalized.startswith(key):
            return key
    return normalized


def _bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KM])", value, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法解析产品容量：{value!r}")
    multiplier = {"K": 1024, "M": 1024 * 1024}[match.group(2).upper()]
    return int(float(match.group(1)) * multiplier)


def _product_url(href: str, part_number: str) -> str:
    url = urllib.parse.urljoin(BASE_URL, href or f"/product/mcu/mcus-product-selector/{part_number.lower()}")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in {"gigadevice.com", "www.gigadevice.com"}:
        raise ValueError(f"产品链接不属于 GigaDevice 官方域名：{url}")
    return urllib.parse.urlunsplit(("https", "www.gigadevice.com", parsed.path, "", ""))


def parse_product_page(html: str) -> list[dict[str, object]]:
    parser = _ProductTableParser()
    parser.feed(html)
    parser.close()
    required = {
        "partno",
        "core",
        "series",
        "package",
        "maxspeedmhz",
        "flashbytes",
        "srambytes",
    }
    header: list[str] | None = None
    products: list[dict[str, object]] = []
    for row in parser.rows:
        keys = [_header_key(cell["text"]) for cell in row]
        if required <= set(keys):
            header = keys
            continue
        if header is None or len(row) < len(header):
            continue
        cells = {key: row[index] for index, key in enumerate(header)}
        part_number = cells["partno"]["text"].upper()
        if not re.fullmatch(r"GD32A7[A-Z0-9]+", part_number):
            continue
        products.append(
            {
                "part_number": part_number,
                "core": cells["core"]["text"],
                "series": cells["series"]["text"].upper(),
                "package": cells["package"]["text"],
                "max_speed_mhz": int(cells["maxspeedmhz"]["text"]),
                "flash_bytes": _bytes(cells["flashbytes"]["text"]),
                "sram_bytes": _bytes(cells["srambytes"]["text"]),
                "url": _product_url(cells["partno"]["href"], part_number),
            }
        )
    by_part = {str(product["part_number"]): product for product in products}
    if len(by_part) != len(products):
        raise ValueError("产品选择器包含重复 GD32A7 料号")
    return [by_part[key] for key in sorted(by_part)]


def _source(html: str) -> dict[str, object]:
    encoded = html.encode("utf-8")
    return {
        "url": SOURCE_PAGE,
        "size": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def write_report(path: Path, html: str, products: list[dict[str, object]]) -> None:
    data = {
        "schema_version": 1,
        "source": _source(html),
        "summary": {
            "products": len(products),
            "series": sorted({str(product["series"]) for product in products}),
        },
        "products": products,
    }
    common._write_text_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def read_product_page() -> str:
    result = subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--max-time",
            "60",
            "--user-agent",
            common.USER_AGENT,
            SOURCE_PAGE,
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache",
        type=Path,
        default=root / ".cache/research/gigadevice/products/gd32a7xx.html",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=root / "sources/gigadevice/products.lock.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "reports/gigadevice-products.json",
    )
    parser.add_argument("--minimum-products", type=int, default=33)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    html = read_product_page()
    products = parse_product_page(html)
    if len(products) < args.minimum_products:
        raise ValueError(
            f"官网仅发现 {len(products)} 个 GD32A7 产品，少于下限 {args.minimum_products}"
        )
    common._write_text_atomic(args.cache, html)
    source = _source(html)
    common._write_text_atomic(
        args.lock,
        json.dumps(
            {
                "schema_version": 1,
                "source": source,
                "products": len(products),
                "redistribution": "原始网页仅作来源缓存；仓库只提交哈希和事实型产品索引。",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    write_report(args.output, html, products)
    print(f"已锁定 {len(products)} 个 GD32A7 官方产品：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, urllib.error.URLError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
