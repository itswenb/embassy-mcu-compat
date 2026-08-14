#!/usr/bin/env python3
"""盘点已锁定 GD32 用户手册中的固定 DMA 请求映射表。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import gigadevice_sources as common


DMA_MAPPING_RE = re.compile(
    r"(?:DMA.*?request|request.*?DMA|DMA.*?channel\s+selection|"
    r"requests?\s+for\s+each\s+channel|DMA.*?请求|"
    r"请求.*?DMA|DMA.*?各通道)",
    re.IGNORECASE,
)
TABLE_RE = re.compile(r"^(?:Table\s+\d+[-–.]\d+\.?|表\s*\d+[-–.]\d+)", re.IGNORECASE)
MAIN_FIXED_RE = re.compile(
    r"Table\s+(?P<number>\d+[-–]\d+)\.\s*"
    r"(?P<controller>DMA\d*)\s+requests?\s+for\s+each\s+channel(?P<scope>.*)$",
    re.IGNORECASE,
)
MAIN_SELECTED_RE = re.compile(
    r"Table\s+(?P<number>\d+[-–]\d+)\.\s*Peripheral\s+requests?\s+to\s+"
    r"(?P<controller>DMA\d*)(?P<scope>.*)$",
    re.IGNORECASE,
)
SECTION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)*\.\s+\S")
SIGNAL_START_RE = re.compile(
    r"(?<!/)(?:ADC\d*|AES\d*|CAN\d*|CAU|DAC\d*|DCI|ENET\d*|EXMC|HAU|"
    r"HASH\d*|HPDF\d*|I2C\d+|I2S\d*(?:ADD)?|JPEG|MFCOM|PDM\d*|QUANSPI|QUADSPI|"
    r"QSPI|SAI\d*|SDIO|SHRTIMER|SPDIF\d*|SPI\d*|TIMER\d+|TMER\d+|TLI|TRNG|"
    r"UART\d+|USART\d+|USB\w*)"
)
TSV_VERSION = 1


def family_matches(family: str, device: str) -> bool:
    """判断手册型号通配符是否覆盖具体芯片型号。"""
    pattern = "".join(
        "[A-Z0-9]" if character.upper() == "X" else re.escape(character.upper())
        for character in family
    )
    return re.match(pattern, device.upper()) is not None


def _main_table_title(line: str) -> dict[str, object] | None:
    normalized = " ".join(line.split())
    if "..." in normalized or " lists " in normalized:
        return None
    match = MAIN_FIXED_RE.match(normalized)
    kind = "fixed"
    if match is None:
        match = MAIN_SELECTED_RE.match(normalized)
        kind = "selected"
    if match is None:
        return None
    scope = match.group("scope")
    if scope.strip() and re.fullmatch(
        r"\s*\(Only\s+for\s+GD32[A-Z0-9x]+\)\s*", scope, re.IGNORECASE
    ) is None:
        return None
    return {
        "number": match.group("number").replace("–", "-"),
        "controller": match.group("controller").upper(),
        "kind": kind,
        "title": normalized,
        "applies_to": re.findall(r"GD32[A-Z0-9x]+", scope, re.IGNORECASE),
    }


def _table_lines(text: str) -> list[dict[str, object]]:
    return [
        {"page": page, "line": line, "text": raw_line}
        for page, raw_page in enumerate(text.split("\f"), 1)
        for line, raw_line in enumerate(raw_page.splitlines(), 1)
    ]


def _header_geometry(line: str) -> tuple[float, list[tuple[int, float]]] | None:
    matches = list(re.finditer(r"Channel\s+(\d+)", line, re.IGNORECASE))
    if len(matches) < 2:
        return None
    channels = [
        (int(match.group(1)), (match.start() + match.end()) / 2) for match in matches
    ]
    prefix = line[: matches[0].start()]
    label = list(re.finditer(r"Peripheral|Channel", prefix, re.IGNORECASE))
    label_center = (
        (label[-1].start() + label[-1].end()) / 2
        if label
        else channels[0][1] - (channels[1][1] - channels[0][1])
    )
    return label_center, channels


def _is_table_end(line: str) -> bool:
    normalized = " ".join(line.split())
    return bool(
        normalized
        and (
            normalized.startswith("Figure ")
            or SECTION_RE.match(normalized) is not None
            or normalized.startswith("Note:")
            or re.match(r"^[12]\.\s+When\b", normalized) is not None
            or (TABLE_RE.match(normalized) is not None and _main_table_title(line) is None)
        )
    )


def _selector_geometry(
    rows: list[dict[str, object]],
    geometry: tuple[float, list[tuple[int, float]]],
) -> tuple[float, list[tuple[int, float]]]:
    label_center, channels = geometry
    candidates = []
    for row in rows:
        line = str(row["text"])
        selector = re.search(r"\b[01]{3}\b", line)
        if selector is None:
            continue
        tokens = list(re.finditer(r"\S+", line[selector.end() :]))
        if len(tokens) != len(channels):
            continue
        candidates.append(
            [
                selector.end() + (token.start() + token.end()) / 2
                for token in tokens
            ]
        )
    if not candidates:
        return geometry
    centers = [
        statistics.median(candidate[index] for candidate in candidates)
        for index in range(len(channels))
    ]
    if any(left >= right for left, right in zip(centers, centers[1:])):
        return geometry
    return label_center, [
        (channel, center) for (channel, _), center in zip(channels, centers, strict=True)
    ]


def _line_pieces(
    row: dict[str, object], label_center: float, channels: list[tuple[int, float]]
) -> list[tuple[int, str, int, int]]:
    line = str(row["text"])
    if (
        len(re.findall(r"Channel\s+\d+", line, re.IGNORECASE)) >= 2
        or "User Manual" in line
        or "用户手册" in line
        or re.fullmatch(r"\s*\d{3,4}\s*", line) is not None
    ):
        return []
    centers = [label_center, *(center for _, center in channels)]
    pieces = []
    for match in re.finditer(r"\S+", line):
        token = match.group()
        if token == "●" or re.fullmatch(r"[01]{3}", token) is not None:
            continue
        center = (match.start() + match.end()) / 2
        closest = min(range(len(centers)), key=lambda index: abs(centers[index] - center))
        if closest == 0:
            continue
        pieces.append(
            (
                channels[closest - 1][0],
                token,
                int(row["page"]),
                int(row["line"]),
            )
        )
    return pieces


def _piece_signals(
    pieces: list[tuple[str, int, int]]
) -> list[dict[str, object]]:
    if not pieces:
        return []
    joined = "".join(piece[0] for piece in pieces)
    starts = list(SIGNAL_START_RE.finditer(joined))
    routes = []
    offsets = []
    position = 0
    for token, page, line in pieces:
        offsets.append((position, position + len(token), page, line))
        position += len(token)
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(joined)
        raw_signal = joined[match.start() : end].strip(".,;:")
        signal = raw_signal
        footnote_match = re.search(r"\(([12])\)$", signal)
        footnote = int(footnote_match.group(1)) if footnote_match else None
        if footnote_match:
            signal = signal[: footnote_match.start()]
        signal = re.sub(r"^TMER(?=\d)", "TIMER", signal)
        sources = [
            (page, line)
            for start, stop, page, line in offsets
            if start < end and stop > match.start()
        ]
        route = {
            "signal": signal,
            "source": {
                "page": min(page for page, _ in sources),
                "line_start": min(line for _, line in sources),
                "line_end": max(line for _, line in sources),
            },
        }
        if footnote is not None:
            route["footnote"] = footnote
        if signal != raw_signal and footnote is None:
            route["raw_signal"] = raw_signal
        elif footnote is not None:
            raw_without_footnote = raw_signal[: footnote_match.start()]
            if signal != raw_without_footnote:
                route["raw_signal"] = raw_without_footnote
        routes.append(route)
    return routes


def _parse_table_routes(
    rows: list[dict[str, object]],
    title: dict[str, object],
    geometry: tuple[float, list[tuple[int, float]]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    label_center, channels = geometry
    issues = []
    grouped: dict[tuple[int, int | None], list[tuple[str, int, int]]] = {}
    selector_limit = int(channels[0][1])
    selector_rows = []
    if title["kind"] == "selected":
        for index, row in enumerate(rows):
            selector = re.search(
                r"\b([01]{3})\b", str(row["text"])[:selector_limit]
            )
            if selector is not None:
                selector_rows.append((index, int(selector.group(1), 2)))
    seen_requests = {request for _, request in selector_rows}
    for index, row in enumerate(rows):
        request = None
        if title["kind"] == "selected":
            if not selector_rows:
                continue
            request = min(selector_rows, key=lambda item: abs(item[0] - index))[1]
        for channel, token, page, line_number in _line_pieces(
            row, label_center, channels
        ):
            grouped.setdefault((channel, request), []).append(
                (token, page, line_number)
            )
    if title["kind"] == "selected" and seen_requests != set(range(8)):
        issues.append(
            {
                "reason": "selector-values-incomplete",
                "table": title["number"],
                "values": sorted(seen_requests),
            }
        )
    routes = []
    for (channel, selected_request), pieces in grouped.items():
        for parsed in _piece_signals(pieces):
            route = {"channel": channel, **parsed}
            if selected_request is not None:
                route["request"] = selected_request
            routes.append(route)
    unique = {}
    for route in routes:
        key = (
            route["channel"],
            route.get("request"),
            route["signal"],
            route.get("footnote"),
        )
        unique.setdefault(key, route)
    routes = sorted(
        unique.values(),
        key=lambda route: (
            int(route["channel"]),
            int(route.get("request", -1)),
            str(route["signal"]),
            int(route.get("footnote", 0)),
        ),
    )
    if not routes:
        issues.append({"reason": "table-routes-empty", "table": title["number"]})
    return routes, issues


def parse_main_tables(
    text: str, manual_name: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """从手册定宽文本中提取可供 metadata 消费的 DMA 路由。"""
    lines = _table_lines(text)
    titles = [
        (index, title)
        for index, row in enumerate(lines)
        if (title := _main_table_title(str(row["text"]))) is not None
    ]
    tables = []
    issues = []
    for title_index, (start, title) in enumerate(titles):
        limit = titles[title_index + 1][0] if title_index + 1 < len(titles) else len(lines)
        header_index = None
        geometry = None
        for index in range(start + 1, min(limit, start + 30)):
            geometry = _header_geometry(str(lines[index]["text"]))
            if geometry is not None:
                header_index = index
                break
        if header_index is None or geometry is None:
            issues.append(
                {
                    "reason": "table-header-missing",
                    "manual": manual_name,
                    "table": title["number"],
                }
            )
            continue
        end = limit
        for index in range(header_index + 1, limit):
            if _is_table_end(str(lines[index]["text"])):
                end = index
                break
        body = lines[header_index + 1 : end]
        if title["kind"] == "selected":
            geometry = _selector_geometry(body, geometry)
        routes, table_issues = _parse_table_routes(body, title, geometry)
        issues.extend({"manual": manual_name, **issue} for issue in table_issues)
        page_end = int(
            lines[end - 1]["page"] if end > header_index + 1 else lines[start]["page"]
        )
        end_marker = None
        if end < len(lines) and int(lines[end]["page"]) == page_end:
            end_marker = " ".join(str(lines[end]["text"]).split())
        tables.append(
            {
                **title,
                "manual": manual_name,
                "page_start": int(lines[start]["page"]),
                "page_end": page_end,
                "end_marker": end_marker,
                "channels": [channel for channel, _ in geometry[1]],
                "routes": routes,
            }
        )
    return tables, issues


def _visual_lines(words: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    lines: list[list[dict[str, object]]] = []
    for word in sorted(words, key=lambda item: (int(item["page"]), float(item["top"]), float(item["left"]))):
        if (
            not lines
            or int(lines[-1][0]["page"]) != int(word["page"])
            or abs(float(lines[-1][0]["top"]) - float(word["top"])) > 4.0
        ):
            lines.append([word])
        else:
            lines[-1].append(word)
    return lines


def _line_text(words: list[dict[str, object]]) -> str:
    return " ".join(
        str(word["text"])
        for word in sorted(words, key=lambda item: float(item["left"]))
    )


def _tsv_header_channels(
    words: list[dict[str, object]], expected: list[int]
) -> list[tuple[int, float]] | None:
    ordered = sorted(words, key=lambda item: float(item["left"]))
    found = []
    for index, word in enumerate(ordered[:-1]):
        following = ordered[index + 1]
        if (
            str(word["text"]).casefold() == "channel"
            and str(following["text"]).isdigit()
        ):
            channel = int(str(following["text"]))
            left = float(word["left"])
            right = float(following["left"]) + float(following["width"])
            found.append((channel, (left + right) / 2))
    if [channel for channel, _ in found] != expected:
        return None
    return found


def _continuation_anchor(
    word: dict[str, object],
    words: list[dict[str, object]],
    channels: list[tuple[int, float]],
) -> dict[str, object] | None:
    text = str(word["text"])
    if text not in {"R", "X"}:
        return None
    left = float(word["left"])
    top = float(word["top"])
    spacing = statistics.median(
        right - left for (_, left), (_, right) in zip(channels, channels[1:])
    )

    def channel_of(item: dict[str, object]) -> int | None:
        center = float(item["left"]) + float(item["width"]) / 2
        if center < channels[0][1] - spacing / 2 or center > channels[-1][1] + spacing / 2:
            return None
        return min(channels, key=lambda channel: abs(channel[1] - center))[0]

    word_channel = channel_of(word)
    candidates = []
    for candidate in words:
        candidate_text = str(candidate["text"])
        if candidate is word or int(candidate["page"]) != int(word["page"]):
            continue
        vertical = top - float(candidate["top"])
        if (
            vertical < -2
            or vertical > 22
            or re.fullmatch(r"[A-Z][A-Z0-9_/]*", candidate_text) is None
            or channel_of(candidate) == word_channel
        ):
            continue
        right = float(candidate["left"]) + float(candidate["width"])
        gap = left - right
        if 0 <= gap <= 20:
            candidates.append((gap, vertical, candidate))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def _request_anchor(
    word: dict[str, object],
    words: list[dict[str, object]],
    channel: int,
    channels: list[tuple[int, float]],
) -> dict[str, object] | None:
    text = str(word["text"])
    if SIGNAL_START_RE.match(text) is not None or re.fullmatch(
        r"[A-Z0-9_()]{1,4}", text
    ) is None:
        return None
    spacing = statistics.median(
        right - left for (_, left), (_, right) in zip(channels, channels[1:])
    )

    def channel_of(item: dict[str, object]) -> int | None:
        center = float(item["left"]) + float(item["width"]) / 2
        if center < channels[0][1] - spacing / 2 or center > channels[-1][1] + spacing / 2:
            return None
        return min(channels, key=lambda candidate: abs(candidate[1] - center))[0]

    center = float(word["left"]) + float(word["width"]) / 2
    candidates = []
    for candidate in words:
        vertical = float(word["top"]) - float(candidate["top"])
        if (
            candidate is word
            or int(candidate["page"]) != int(word["page"])
            or vertical < -2
            or vertical > 25
            or channel_of(candidate) != channel
            or SIGNAL_START_RE.match(str(candidate["text"])) is None
        ):
            continue
        candidate_center = float(candidate["left"]) + float(candidate["width"]) / 2
        candidates.append((vertical, abs(center - candidate_center), candidate))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def parse_tsv_table_routes(
    words: list[dict[str, object]], table: dict[str, object]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """按 PDF 词坐标重建单张 DMA 主表的单元格。"""
    issues = []
    lines = _visual_lines(words)
    title_prefix = f"Table {table['number']}."
    title_line = next(
        (
            line
            for line in lines
            if int(line[0]["page"]) == int(table["page_start"])
            and _line_text(line).startswith(title_prefix)
        ),
        None,
    )
    if title_line is None:
        return [], [{"reason": "tsv-table-title-missing", "table": table["number"]}]
    title_top = float(title_line[0]["top"])
    end_top = None
    end_marker = table.get("end_marker")
    if end_marker:
        normalized_marker = " ".join(str(end_marker).split())
        marker_line = next(
            (
                line
                for line in lines
                if int(line[0]["page"]) == int(table["page_end"])
                and _line_text(line).startswith(normalized_marker)
            ),
            None,
        )
        if marker_line is not None:
            end_top = float(marker_line[0]["top"])
        else:
            return [], [
                {"reason": "tsv-table-end-missing", "table": table["number"]}
            ]

    scoped_lines = [
        line
        for line in lines
        if int(table["page_start"]) <= int(line[0]["page"]) <= int(table["page_end"])
        and (
            int(line[0]["page"]) != int(table["page_start"])
            or float(line[0]["top"]) > title_top
        )
        and (
            end_top is None
            or int(line[0]["page"]) != int(table["page_end"])
            or float(line[0]["top"]) < end_top
        )
    ]
    expected_channels = list(map(int, table["channels"]))
    header_by_page = {}
    for line in scoped_lines:
        channels = _tsv_header_channels(line, expected_channels)
        if channels is not None:
            header_by_page.setdefault(
                int(line[0]["page"]), (float(line[0]["top"]), channels)
            )
    if int(table["page_start"]) not in header_by_page:
        return [], [{"reason": "tsv-table-header-missing", "table": table["number"]}]
    default_channels = header_by_page[int(table["page_start"])][1]
    content = []
    for word in words:
        page = int(word["page"])
        if page < int(table["page_start"]) or page > int(table["page_end"]):
            continue
        top = float(word["top"])
        if page == int(table["page_start"]) and top <= title_top:
            continue
        if page == int(table["page_end"]) and end_top is not None and top >= end_top:
            continue
        header = header_by_page.get(page)
        if header is not None and top <= header[0] + 2.5:
            continue
        if str(word["text"]) in {"###PAGE###", "###FLOW###", "###LINE###"}:
            continue
        text = str(word["text"])
        if (
            text.startswith("GD32")
            or text in {"User", "Manual"}
            or (
                text.isdigit()
                and len(text) >= 3
                and re.fullmatch(r"[01]{3}", text) is None
            )
        ):
            continue
        content.append(word)

    selectors = []
    if table["kind"] == "selected":
        for word in content:
            text = str(word["text"])
            page = int(word["page"])
            channels = header_by_page.get(page, (0.0, default_channels))[1]
            spacing = statistics.median(
                right - left
                for (_, left), (_, right) in zip(channels, channels[1:])
            )
            center = float(word["left"]) + float(word["width"]) / 2
            if re.fullmatch(r"[01]{3}", text) and center < channels[0][1] - spacing / 2:
                selectors.append((page, float(word["top"]), int(text, 2)))
        if not selectors:
            return [], [{"reason": "tsv-selector-values-missing", "table": table["number"]}]

    blocked_cells = set()
    if table["kind"] == "selected":
        for word in content:
            if str(word["text"]) != "●":
                continue
            page = int(word["page"])
            channels = header_by_page.get(page, (0.0, default_channels))[1]
            center = float(word["left"]) + float(word["width"]) / 2
            channel, _ = min(channels, key=lambda item: abs(item[1] - center))
            page_selectors = [selector for selector in selectors if selector[0] == page]
            if page_selectors:
                request = min(
                    page_selectors,
                    key=lambda selector: abs(selector[1] - float(word["top"])),
                )[2]
                blocked_cells.add((page, request, channel))

    grouped: dict[tuple[int, int | None], list[dict[str, object]]] = {}
    for word in content:
        text = str(word["text"])
        if text == "●" or re.fullmatch(r"[01]{3}", text):
            continue
        page = int(word["page"])
        channels = header_by_page.get(page, (0.0, default_channels))[1]
        spacing = statistics.median(
            right - left for (_, left), (_, right) in zip(channels, channels[1:])
        )
        anchor = _continuation_anchor(word, content, channels)
        positioned = anchor if anchor is not None else word
        center = float(positioned["left"]) + float(positioned["width"]) / 2
        if center < channels[0][1] - spacing / 2 or center > channels[-1][1] + spacing / 2:
            continue
        channel, _ = min(channels, key=lambda item: abs(item[1] - center))
        request = None
        if table["kind"] == "selected":
            request_positioned = _request_anchor(
                word, content, channel, channels
            ) or positioned
            page_selectors = [selector for selector in selectors if selector[0] == page]
            page_selectors = [
                selector
                for selector in page_selectors
                if (page, selector[2], channel) not in blocked_cells
            ]
            if not page_selectors:
                continue
            request = min(
                page_selectors,
                key=lambda selector: abs(
                    selector[1] - float(request_positioned["top"])
                ),
            )[2]
        grouped.setdefault((channel, request), []).append(word)

    routes = []
    for (channel, request), cell_words in grouped.items():
        ordered_words = [
            word
            for line in _visual_lines(cell_words)
            for word in sorted(line, key=lambda item: float(item["left"]))
        ]
        pieces = [
            (
                str(word["text"]),
                int(word["page"]),
                int(round(float(word["top"]) * 100)),
            )
            for word in ordered_words
        ]
        parsed_signals = _piece_signals(pieces)
        raw = "".join(piece[0] for piece in pieces)
        if not parsed_signals:
            if raw:
                issues.append(
                    {
                        "reason": "tsv-cell-unparsed",
                        "table": table["number"],
                        "channel": channel,
                        "request": request,
                        "text": raw[:120],
                    }
                )
        for parsed in parsed_signals:
            route = {"channel": channel, **parsed}
            route["source"] = {
                "page": parsed["source"]["page"],
                "cell_text": raw,
            }
            if request is not None:
                route["request"] = request
            routes.append(route)
    unique = {}
    for route in routes:
        key = (
            route["channel"],
            route.get("request"),
            route["signal"],
            route.get("footnote"),
        )
        unique.setdefault(key, route)
    routes = sorted(
        unique.values(),
        key=lambda route: (
            int(route["channel"]),
            int(route.get("request", -1)),
            str(route["signal"]),
            int(route.get("footnote", 0)),
        ),
    )
    if not routes:
        issues.append({"reason": "tsv-table-routes-empty", "table": table["number"]})
    return routes, issues


def pdftotext_identity(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "-v"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = result.stdout.splitlines()
    if not lines:
        raise ValueError("pdftotext 未返回版本信息")
    return lines[0]


def convert_pdf_tsv(
    binary: Path, source: Path, output: Path, first_page: int, last_page: int
) -> None:
    subprocess.run(
        [
            str(binary),
            "-f",
            str(first_page),
            "-l",
            str(last_page),
            "-tsv",
            "-enc",
            "UTF-8",
            "-eol",
            "unix",
            str(source),
            str(output),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def read_tsv_words(source: Path) -> list[dict[str, object]]:
    words = []
    with source.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file, delimiter="\t"):
            if row.get("level") != "5" or str(row.get("text", "")).startswith("###"):
                continue
            words.append(
                {
                    "page": int(row["page_num"]),
                    "left": float(row["left"]),
                    "top": float(row["top"]),
                    "width": float(row["width"]),
                    "height": float(row["height"]),
                    "text": row["text"],
                }
            )
    if not words:
        raise ValueError(f"PDF TSV 不包含文字：{source}")
    return words


def enrich_inventory_from_pdfs(
    inventory: dict[str, object],
    lock: dict[str, object],
    pdf_dir: Path,
    tsv_dir: Path,
    binary: Path,
    identity: str,
) -> None:
    locked = lock.get("manuals")
    if lock.get("schema_version") != 1 or not isinstance(locked, list):
        raise ValueError("用户手册锁文件格式无效")
    by_name = {str(row["name"]): row for row in locked if isinstance(row, dict)}
    if len(by_name) != len(locked):
        raise ValueError("用户手册锁文件名称重复")
    tool_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    for manual in inventory["manuals"]:
        tables = manual["tables"]
        if not tables:
            continue
        locked_manual = by_name.get(str(manual["name"]))
        if locked_manual is None:
            raise ValueError(f"用户手册未锁定：{manual['name']}")
        pdf = pdf_dir / str(locked_manual["filename"])
        pdf_sha256 = str(locked_manual["sha256"])
        if not pdf.is_file() or common._sha256(pdf) != pdf_sha256:
            raise ValueError(f"用户手册 PDF 缺失或哈希不匹配：{pdf}")
        first_page = min(int(table["page_start"]) for table in tables)
        last_page = max(int(table["page_end"]) for table in tables)
        cache = tsv_dir / (
            f"v{TSV_VERSION}-{pdf_sha256[:16]}-{tool_key}-{first_page}-{last_page}.tsv"
        )
        if not cache.is_file():
            tsv_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=tsv_dir, prefix=".manual-dma.") as directory:
                temporary = Path(directory) / "manual.tsv"
                convert_pdf_tsv(binary, pdf, temporary, first_page, last_page)
                if not temporary.is_file():
                    raise ValueError(f"pdftotext 未生成 TSV：{pdf.name}")
                temporary.replace(cache)
        words = read_tsv_words(cache)
        manual["issues"] = []
        for table in tables:
            routes, issues = parse_tsv_table_routes(words, table)
            table["routes"] = routes
            table["extraction"] = "pdftotext-tsv"
            manual["issues"].extend(
                {"manual": manual["name"], **issue} for issue in issues
            )
        manual["pdf"] = {"filename": pdf.name, "sha256": pdf_sha256}
        manual["tsv"] = {
            "cache": cache.name,
            "sha256": common._sha256(cache),
            "first_page": first_page,
            "last_page": last_page,
        }
    summary = inventory["summary"]
    summary["routes"] = sum(
        len(table["routes"])
        for manual in inventory["manuals"]
        for table in manual["tables"]
    )
    summary["parse_issues"] = sum(
        len(manual["issues"]) for manual in inventory["manuals"]
    )


def _matches(text: str) -> list[dict[str, object]]:
    matches = []
    for page, raw_page in enumerate(text.split("\f"), 1):
        for line, raw_line in enumerate(raw_page.splitlines(), 1):
            normalized = " ".join(raw_line.split())
            is_table = TABLE_RE.match(normalized) is not None
            if normalized and (
                DMA_MAPPING_RE.search(normalized) is not None
                or (
                    is_table
                    and "DMA" in normalized.upper()
                    and re.search(
                        r"request|channel|peripheral|stream|请求|通道|外设",
                        normalized,
                        re.IGNORECASE,
                    )
                    is not None
                )
            ):
                matches.append(
                    {
                        "page": page,
                        "line": line,
                        "text": normalized,
                        "kind": "table" if is_table else "heading",
                    }
                )
    return matches


def build_inventory(
    manual_report: dict[str, object], text_dir: Path
) -> dict[str, object]:
    manuals = manual_report.get("manuals")
    if manual_report.get("schema_version") != 1 or not isinstance(manuals, list):
        raise ValueError("用户手册文本报告格式无效")
    rows = []
    for manual in manuals:
        if not isinstance(manual, dict):
            raise ValueError("用户手册文本报告包含非法条目")
        cache_name = str(manual["text_cache"])
        if Path(cache_name).name != cache_name:
            raise ValueError(f"用户手册文本缓存名非法：{cache_name}")
        source = text_dir / cache_name
        if not source.is_file():
            raise ValueError(f"用户手册文本缓存缺失：{source}")
        if common._sha256(source) != str(manual["text_sha256"]):
            raise ValueError(f"用户手册文本哈希不匹配：{source.name}")
        text = source.read_text(encoding="utf-8")
        matches = _matches(text)
        tables, issues = parse_main_tables(text, str(manual["name"]))
        rows.append(
            {
                "name": manual["name"],
                "text_cache": cache_name,
                "text_sha256": manual["text_sha256"],
                "mapping_candidates": matches,
                "table_candidates": [
                    match for match in matches if match["kind"] == "table"
                ],
                "tables": tables,
                "issues": issues,
            }
        )
    return {
        "schema_version": 1,
        "summary": {
            "manuals": len(rows),
            "manuals_with_dma_mapping": sum(
                bool(row["mapping_candidates"]) for row in rows
            ),
            "manuals_with_dma_tables": sum(bool(row["table_candidates"]) for row in rows),
            "mapping_candidates": sum(len(row["mapping_candidates"]) for row in rows),
            "table_candidates": sum(len(row["table_candidates"]) for row in rows),
            "main_tables": sum(len(row["tables"]) for row in rows),
            "fixed_tables": sum(
                table["kind"] == "fixed" for row in rows for table in row["tables"]
            ),
            "selected_tables": sum(
                table["kind"] == "selected"
                for row in rows
                for table in row["tables"]
            ),
            "routes": sum(
                len(table["routes"]) for row in rows for table in row["tables"]
            ),
            "parse_issues": sum(len(row["issues"]) for row in rows),
        },
        "manuals": rows,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    default_binary = shutil.which("pdftotext")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manual-report",
        type=Path,
        default=repo_root / "reports/gigadevice-manuals.json",
    )
    parser.add_argument(
        "--text-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/manual-text-v1",
    )
    parser.add_argument(
        "--manual-lock",
        type=Path,
        default=repo_root / "sources/gigadevice/manuals.lock.json",
    )
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/manuals",
    )
    parser.add_argument(
        "--tsv-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/manual-dma-tsv-v1",
    )
    parser.add_argument(
        "--pdftotext",
        type=Path,
        default=Path(default_binary) if default_binary else None,
        required=default_binary is None,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-manual-dma.json",
    )
    parser.add_argument("--show-manual")
    parser.add_argument("--show-page", type=int)
    parser.add_argument("--minimum-main-tables", type=int, default=40)
    parser.add_argument("--minimum-routes", type=int, default=1500)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    inventory = build_inventory(
        json.loads(args.manual_report.read_text(encoding="utf-8")), args.text_dir
    )
    if (args.show_manual is None) != (args.show_page is None):
        raise ValueError("--show-manual 与 --show-page 必须同时使用")
    if args.show_manual is not None:
        row = next(
            (
                item
                for item in inventory["manuals"]
                if item["name"] == args.show_manual
            ),
            None,
        )
        if row is None:
            raise ValueError(f"未找到用户手册：{args.show_manual}")
        pages = (args.text_dir / row["text_cache"]).read_text(encoding="utf-8").split("\f")
        if args.show_page < 1 or args.show_page > len(pages):
            raise ValueError(f"用户手册页码越界：{args.show_page}")
        print(pages[args.show_page - 1])
        return 0
    identity = pdftotext_identity(args.pdftotext)
    enrich_inventory_from_pdfs(
        inventory,
        json.loads(args.manual_lock.read_text(encoding="utf-8")),
        args.pdf_dir,
        args.tsv_dir,
        args.pdftotext,
        identity,
    )
    inventory["provenance"] = {
        "manual_report": {
            "path": args.manual_report.name,
            "sha256": common._sha256(args.manual_report),
        },
        "manual_lock": {
            "path": args.manual_lock.name,
            "sha256": common._sha256(args.manual_lock),
        },
        "pdftotext": identity,
    }
    common._write_text_atomic(
        args.output,
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = inventory["summary"]
    if int(summary["main_tables"]) < args.minimum_main_tables:
        raise ValueError("DMA 手册主表数量低于覆盖门限")
    if int(summary["routes"]) < args.minimum_routes:
        raise ValueError("DMA 手册路由数量低于覆盖门限")
    if int(summary["parse_issues"]) != 0:
        raise ValueError("DMA 手册主表存在未解析单元格")
    for manual in inventory["manuals"]:
        for table in manual["tables"]:
            for route in table["routes"]:
                if re.fullmatch(r"[A-Z0-9_/]+", str(route["signal"])) is None:
                    raise ValueError(
                        f"DMA 手册路由信号非法：{manual['name']}: {route['signal']}"
                    )
                if table["kind"] == "selected" and not 0 <= int(route["request"]) <= 7:
                    raise ValueError(
                        f"DMA 手册路由选择码越界：{manual['name']}: {route['request']}"
                    )
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"DMA 手册表盘点：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
