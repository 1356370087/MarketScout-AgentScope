"""Structured parse products: rich units, table row groups and segment plans.

Docling JSON, enhanced XLSX/CSV reads and light text structure all normalize
into :class:`StructuredUnit` objects. Tables are indexed as row-group
segments that repeat their header and footnote lines; adjacent-page tables
with matching column structure are marked as merge candidates instead of
being silently merged (plan §KB-06).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes

from .chunking import _windows
from .parsers import DocumentParseError, ParsedDocument
from .settings import DocumentSettings

_TABLE_LABEL = "table"
_TEXTUAL_LABELS = {
    "title": "title",
    "section_header": "section_header",
    "paragraph": "paragraph",
    "text": "paragraph",
    "list_item": "list_item",
    "caption": "caption",
    "page_header": "page_header",
    "page_footer": "page_footer",
    "formula": "paragraph",
}
_IGNORED_LABELS = {"picture", "figure", "reference", "formula"}  # formula→md text only
_UNIT_TYPES = frozenset(
    {"title", "section_header", "paragraph", "table", "caption", "page_header",
     "page_footer", "list_item", "notes"}
)


@dataclass(slots=True)
class StructuredUnit:
    """One structural element ready for persistence and indexing."""

    unit_type: str
    locator: dict[str, Any]
    raw_text: str
    index_text: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Collapse unknown unit labels onto the paragraph type."""
        if self.unit_type not in _UNIT_TYPES:
            self.unit_type = "paragraph"


@dataclass(slots=True)
class PreparedDocument:
    """Everything the worker needs to complete one generation."""

    units: list[StructuredUnit] = field(default_factory=list)
    # segment i belongs to units[segment_units[i]]; its text is segment_texts[i].
    segment_texts: list[str] = field(default_factory=list)
    segment_units: list[int] = field(default_factory=list)
    page_count: int | None = None
    ocr_pages: int = 0
    quality_flags: list[str] = field(default_factory=list)
    parse_method: str = "legacy"
    upstream_task_id: str | None = None


def _cell_text(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, dict):
        text = str(cell.get("text") or "")
    else:
        text = str(cell)
    return text.replace(" ", " ").strip()


def _table_grid(table_item: dict[str, Any]) -> list[list[str]]:
    data = table_item.get("data") or {}
    grid = data.get("grid") or []
    if grid:
        return [[_cell_text(cell) for cell in row] for row in grid]
    num_rows = int(data.get("num_rows") or 0)
    num_cols = int(data.get("num_cols") or 0)
    cells = data.get("table_cells") or []
    matrix = [[""] * max(num_cols, 1) for _ in range(max(num_rows, 1))]
    for cell in cells:
        try:
            row = int(cell.get("start_row_offset_idx", 0))
            col = int(cell.get("start_col_offset_idx", 0))
        except (TypeError, ValueError):
            continue
        if 0 <= row < len(matrix) and 0 <= col < len(matrix[0]):
            matrix[row][col] = _cell_text(cell.get("text"))
    return matrix


def _table_annotation(table_item: dict[str, Any], key: str) -> str:
    annotations = table_item.get(key) or []
    if isinstance(annotations, dict):
        annotations = [annotations]
    parts = []
    for annotation in annotations:
        if isinstance(annotation, dict):
            parts.append(str(annotation.get("text") or "").strip())
        elif annotation:
            parts.append(str(annotation).strip())
    return "\n".join(part for part in parts if part)


def _table_unit(
    grid: list[list[str]],
    *,
    page: int | None,
    locator_extra: dict[str, Any],
    attributes: dict[str, Any],
) -> StructuredUnit:
    header = [cell for cell in (grid[0] if grid else [])]
    index_text = "\n".join(" | ".join(row) for row in grid if any(row))
    locator: dict[str, Any] = {"source": locator_extra.get("source", "table")}
    if page is not None:
        locator["page"] = page
    if "bbox" in locator_extra:
        locator["bbox"] = locator_extra["bbox"]
    for key in ("sheet", "rows"):
        if key in locator_extra:
            locator[key] = locator_extra[key]
    attributes = {
        "num_rows": len(grid),
        "num_cols": max((len(row) for row in grid), default=0),
        "header": header,
        **attributes,
    }
    return StructuredUnit(
        unit_type="table",
        locator=locator,
        raw_text=index_text,
        index_text=index_text,
        attributes=attributes,
    )


def row_group_texts(
    grid: list[list[str]], footnotes: str = "", group_rows: int = 40
) -> list[str]:
    """Split one table into row-group texts repeating header and footnotes."""
    if not grid:
        return []
    header = grid[0]
    header_line = " | ".join(header)
    body = [row for row in grid[1:] if any(row)]
    if not body:
        return ["\n".join(row for row in grid if any(row))]
    groups: list[str] = []
    for start in range(0, len(body), max(1, group_rows)):
        chunk = body[start : start + max(1, group_rows)]
        lines = [header_line, *((" | ".join(row)) for row in chunk)]
        if footnotes:
            lines.append(footnotes)
        groups.append("\n".join(lines))
    return groups


def plan_table_segments(unit: StructuredUnit, group_rows: int) -> list[str]:
    """Row-group index texts for one table unit."""
    footnotes = str(unit.attributes.get("footnotes") or "")
    unit_note = str(unit.attributes.get("unit_note") or "")
    note = "\n".join(part for part in (footnotes, unit_note) if part)
    grid: list[list[str]] = [[*unit.attributes.get("header", [])]]
    raw_lines = [line for line in unit.raw_text.split("\n") if line.strip()]
    for line in raw_lines[1:]:
        grid.append([cell.strip() for cell in line.split(" | ")])
    grid = [row for row in grid if any(cell for cell in row)]
    return row_group_texts(grid, footnotes=note, group_rows=group_rows)


def mark_merge_candidates(units: list[StructuredUnit]) -> list[str]:
    """Flag adjacent-page tables with matching column counts for review.

    Returns the marker names added. Original per-page tables are always
    kept; ``merge_candidate`` only suggests a human-confirmed merge.
    """
    markers: list[str] = []
    tables = [(index, unit) for index, unit in enumerate(units) if unit.unit_type == "table"]
    for (left_index, left), (right_index, right) in zip(tables, tables[1:], strict=False):
        left_page = left.locator.get("page")
        right_page = right.locator.get("page")
        if left_page is None or right_page is None or right_page - left_page != 1:
            continue
        if left.attributes.get("num_cols") != right.attributes.get("num_cols"):
            continue
        left_header = [str(cell).strip().lower() for cell in left.attributes.get("header", [])]
        right_header = [str(cell).strip().lower() for cell in right.attributes.get("header", [])]
        header_repeat = bool(left_header) and left_header == right_header
        continuation = bool(
            re.search(r"续表|续上表|continued", right.index_text[:200], re.IGNORECASE)
        )
        if not (header_repeat or continuation):
            continue
        marker = f"merge_candidate:{left_index}:{right_index}"
        for unit in (left, right):
            unit.attributes.setdefault("merge_candidates", []).append(marker)
        markers.append(marker)
    return markers


def _prov_location(item: dict[str, Any]) -> tuple[int | None, list[float] | None]:
    """Return (page_no, bbox) from one item's prov list."""
    provs = item.get("prov") or []
    if not provs:
        return None, None
    first = provs[0] if isinstance(provs[0], dict) else {}
    page_no = int(first["page_no"]) if first.get("page_no") else None
    bbox = None
    if isinstance(first.get("bbox"), dict):
        box = first["bbox"]
        try:
            bbox = [float(box.get(k, 0.0)) for k in ("l", "t", "r", "b")]
        except (TypeError, ValueError):
            bbox = None
    return page_no, bbox


def _text_of(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, dict):
        value = str(text.get("text") or "")
    else:
        value = str(item.get("orig") or "")
    # Docling emits non-breaking spaces between words; normalize for matching.
    return value.replace(" ", " ").strip()


def from_docling(result_body: dict[str, Any], *, parse_method: str = "docling") -> PreparedDocument:
    """Convert one Docling Serve result into structured units.

    Supports both the docling-core 2.x model (separate ``texts``/``tables``/
    ``key_value_items`` arrays, text under ``orig``) and the older flat
    ``items`` export.
    """
    document = (result_body.get("document") or {}).get("json_content") or {}
    quality_flags: list[str] = []
    status = str(result_body.get("status") or "success")
    if status == "partial_success":
        quality_flags.append("docling_partial_success")

    texts = document.get("texts") or []
    tables = document.get("tables") or []
    key_values = document.get("key_value_items") or []
    pictures = document.get("pictures") or []
    if pictures:
        quality_flags.extend(["picture_extracted"] * min(len(pictures), 5))
    if not texts and not tables and not key_values and document.get("items"):
        return _from_docling_flat_items(result_body, document, parse_method)

    entries: list[tuple[int, float, int, str, dict[str, Any]]] = []
    sequence = 0
    for item in texts:
        text = _text_of(item)
        if not text:
            continue
        label = str(item.get("label") or "").lower()
        page_no, bbox = _prov_location(item)
        entries.append((page_no or 0, (bbox or [0, 0, 0, 0])[1], sequence, "text", item))
        sequence += 1
    for item in tables:
        page_no, bbox = _prov_location(item)
        entries.append((page_no or 0, (bbox or [0, 0, 0, 0])[1], sequence, "table", item))
        sequence += 1
    for item in key_values:
        page_no, bbox = _prov_location(item)
        entries.append((page_no or 0, (bbox or [0, 0, 0, 0])[1], sequence, "key_value", item))
        sequence += 1
    # docling bbox.t grows towards the top of the page: pages ascending,
    # top edge descending within a page, stable by emission sequence.
    entries.sort(key=lambda entry: (entry[0], -entry[1], entry[2]))

    units: list[StructuredUnit] = []
    for page_no, _top, _sequence, kind, item in entries:
        if kind == "table":
            # New-shape items carry ``data`` at the top level; _table_grid
            # unwraps it itself (old shape wrapped it under ``table``).
            grid = _table_grid(item)
            if not any(any(row) for row in grid):
                continue
            footnotes = _table_annotation(item, "footnotes")
            captions = _table_annotation(item, "captions")
            source = f"table:{len(units) + 1}" + (f":page:{page_no}" if page_no else "")
            _, table_bbox = _prov_location(item)
            unit = _table_unit(
                grid,
                page=page_no or None,
                locator_extra={"source": source, "bbox": table_bbox},
                attributes={
                    "parse_method": parse_method,
                    "footnotes": footnotes,
                    "captions": captions,
                },
            )
            if captions:
                unit.attributes["unit_note"] = captions
            units.append(unit)
            continue
        if kind == "key_value":
            key = _text_of(item.get("key") or {}) if isinstance(item.get("key"), dict) else str(item.get("key") or "").strip()
            raw_value = item.get("value")
            if isinstance(raw_value, list):
                value = " ".join(
                    part
                    for part in (
                        _text_of(element) if isinstance(element, dict) else str(element or "")
                        for element in raw_value
                    )
                    if part
                )
            elif isinstance(raw_value, dict):
                value = _text_of(raw_value)
            else:
                value = str(raw_value or "").strip()
            if not key and not value:
                continue
            units.append(
                StructuredUnit(
                    unit_type="paragraph",
                    locator={
                        "source": f"key_value:{len(units) + 1}",
                        "page": page_no or None,
                    },
                    raw_text=f"{key}：{value}",
                    index_text=f"{key}：{value}",
                    attributes={"parse_method": parse_method, "kv_key": key},
                )
            )
            continue
        text = _text_of(item)
        label = str(item.get("label") or "").lower()
        unit_type = _TEXTUAL_LABELS.get(label, "paragraph")
        _, bbox = _prov_location(item)
        units.append(
            StructuredUnit(
                unit_type=unit_type,
                locator={
                    "source": f"{label}:{len(units) + 1}",
                    "page": page_no or None,
                    "bbox": bbox,
                },
                raw_text=text,
                index_text=text,
                attributes={"parse_method": parse_method},
            )
        )
    markers = mark_merge_candidates(units)
    quality_flags.extend(markers)
    pages = document.get("pages") or {}
    return PreparedDocument(
        units=units,
        page_count=len(pages) or None,
        quality_flags=quality_flags,
        parse_method=parse_method,
    )


def _from_docling_flat_items(
    result_body: dict[str, Any], document: dict[str, Any], parse_method: str
) -> PreparedDocument:
    """Legacy flat ``items`` export (docling-core 1.x compatibility)."""
    items = document.get("items") or []
    units: list[StructuredUnit] = []
    quality_flags: list[str] = []
    status = str(result_body.get("status") or "success")
    if status == "partial_success":
        quality_flags.append("docling_partial_success")
    for item in items:
        label = str(item.get("label") or "").lower()
        provs = item.get("prov") or []
        page_no = int(provs[0].get("page_no")) if provs and provs[0].get("page_no") else None
        bbox = None
        if provs and isinstance(provs[0].get("bbox"), dict):
            box = provs[0]["bbox"]
            try:
                bbox = [float(box.get(k, 0.0)) for k in ("l", "t", "r", "b")]
            except (TypeError, ValueError):
                bbox = None
        if label == _TABLE_LABEL:
            payload = item.get("table") or item
            grid = _table_grid(payload)
            if not any(any(row) for row in grid):
                continue
            source = f"table:{len(units) + 1}" + (f":page:{page_no}" if page_no else "")
            footnotes = _table_annotation(payload, "footnotes")
            captions = _table_annotation(payload, "captions")
            unit = _table_unit(
                grid,
                page=page_no,
                locator_extra={"source": source, "bbox": bbox},
                attributes={
                    "parse_method": parse_method,
                    "footnotes": footnotes,
                    "captions": captions,
                },
            )
            if captions:
                unit.attributes["unit_note"] = captions
            units.append(unit)
            continue
        if label in {"picture", "figure"}:
            quality_flags.append("picture_extracted")
            continue
        if label in _IGNORED_LABELS:
            continue
        unit_type = _TEXTUAL_LABELS.get(label, "paragraph")
        text = _text_of(item)
        if not text:
            continue
        source = f"{label}:{len(units) + 1}" + (f":page:{page_no}" if page_no else "")
        units.append(
            StructuredUnit(
                unit_type=unit_type,
                locator={"source": source, "page": page_no, "bbox": bbox},
                raw_text=text,
                index_text=text,
                attributes={"parse_method": parse_method},
            )
        )
    markers = mark_merge_candidates(units)
    if markers:
        quality_flags.extend(markers)
    pages = document.get("pages") or {}
    return PreparedDocument(
        units=units,
        page_count=len(pages) or None,
        quality_flags=quality_flags,
        parse_method=parse_method,
    )


def from_parsed_document(
    parsed: ParsedDocument, *, parse_method: str = "legacy"
) -> PreparedDocument:
    """Wrap the legacy flat parser result into structured units."""
    units = [
        StructuredUnit(
            unit_type="paragraph",
            locator={"source": unit.locator, "heading": unit.heading},
            raw_text=unit.text,
            index_text=f"{unit.heading}\n{unit.text}".strip() if unit.heading else unit.text,
            attributes={"parse_method": parse_method},
        )
        for unit in parsed.units
    ]
    return PreparedDocument(
        units=units,
        page_count=parsed.page_count,
        ocr_pages=parsed.ocr_pages,
        quality_flags=list(parsed.quality_flags),
        parse_method=parse_method,
    )


def _decode_text(path: Path) -> str:
    match = from_bytes(path.read_bytes()).best()
    if match is None:
        raise DocumentParseError("document_text_encoding_unknown")
    return str(match)


def _column_type(samples: list[str]) -> str:
    non_empty = [value for value in samples if value]
    if not non_empty:
        return "text"
    if all(re.fullmatch(r"-?\d+", value) for value in non_empty):
        return "integer"
    if all(re.fullmatch(r"-?\d+(\.\d+)?%?", value) for value in non_empty):
        return "number"
    if all(
        re.fullmatch(r"\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?", value) for value in non_empty
    ):
        return "date"
    return "text"


def from_xlsx(path: Path, settings: DocumentSettings) -> PreparedDocument:
    """Dual-read every sheet: formulas from the raw workbook, cached values too.

    Cells keep ``formula`` and ``cached_value`` side by side; missing cached
    results are listed rather than recomputed (no macro or recalculation).
    """
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    try:
        formulas = load_workbook(path, read_only=True, data_only=False)
        values = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise DocumentParseError("document_xlsx_invalid") from exc
    units: list[StructuredUnit] = []
    quality_flags: list[str] = []
    try:
        if len(formulas.sheetnames) > settings.max_logical_units:
            raise DocumentParseError("document_logical_unit_limit_exceeded")
        for sheet_name in formulas.sheetnames:
            formula_sheet = formulas[sheet_name]
            value_sheet = values[sheet_name]
            grid: list[list[str]] = []
            inference: list[list[str]] = []  # raw values only, no annotations
            formula_map: dict[str, str] = {}
            missing_cache: list[str] = []
            external_links = False
            merged: list[str] = []
            merged_ranges = getattr(formula_sheet, "merged_cells", None)
            if merged_ranges is not None and hasattr(merged_ranges, "ranges"):
                merged = [str(merged_range) for merged_range in merged_ranges.ranges]
            row_index = 0
            for formula_row, value_row in zip(
                formula_sheet.iter_rows(), value_sheet.iter_rows(), strict=False
            ):
                row_index += 1
                if row_index > 100_000:
                    raise DocumentParseError("document_row_limit_exceeded")
                rendered: list[str] = []
                raw_values: list[str] = []
                row_has_content = False
                for column_index, (cell, cached) in enumerate(
                    zip(formula_row, value_row, strict=False), 1
                ):
                    reference = (
                        cell.coordinate
                        if hasattr(cell, "coordinate")
                        else f"{get_column_letter(column_index)}{row_index}"
                    )
                    raw = cell.value
                    shown = cached.value if cached is not None else None
                    if isinstance(raw, str) and raw.startswith("="):
                        formula_map[reference] = raw
                        if shown is None:
                            missing_cache.append(reference)
                            rendered.append(f"{raw}（未提供计算结果）")
                            raw_values.append("")  # no cached number to infer from
                        else:
                            rendered.append(_render_cell(shown))
                            raw_values.append(_render_cell(shown))
                        row_has_content = True
                        continue
                    if isinstance(raw, str) and raw.startswith(("=[", "=WEBSERVICE")):
                        external_links = True
                    if raw is not None and shown is None and not isinstance(raw, str):
                        # Read-only mode hides some cached scalars; trust raw.
                        shown = raw
                    value = _render_cell(shown if shown is not None else raw)
                    rendered.append(value)
                    raw_values.append(value)
                    if shown is not None or raw is not None:
                        row_has_content = True
                if row_has_content and any(cell.strip() for cell in rendered):
                    grid.append(rendered)
                    inference.append(raw_values)
            if not grid:
                continue
            attributes: dict[str, Any] = {
                "parse_method": "openpyxl",
                "sheet": sheet_name,
                "column_types": {
                    header or f"col_{index + 1}": _column_type(
                        [row[index] if index < len(row) else "" for row in inference[1:]]
                    )
                    for index, header in enumerate(grid[0])
                },
            }
            if formula_map:
                attributes["formulas"] = formula_map
            if missing_cache:
                attributes["missing_cache"] = missing_cache
                quality_flags.append(f"missing_cached_values:{sheet_name}:{len(missing_cache)}")
            if merged:
                attributes["merged_cells"] = merged
            if external_links:
                attributes["external_links"] = True
                quality_flags.append(f"external_links_present:{sheet_name}")
            unit = _table_unit(
                grid,
                page=None,
                locator_extra={"source": f"sheet:{sheet_name}", "sheet": sheet_name},
                attributes=attributes,
            )
            units.append(unit)
    finally:
        formulas.close()
        values.close()
    return PreparedDocument(
        units=units,
        page_count=len(formulas.sheetnames),
        quality_flags=quality_flags,
        parse_method="openpyxl",
    )


def _render_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def from_csv(path: Path) -> PreparedDocument:
    """Structured CSV parse with per-column type suggestions."""
    reader = csv.reader(io.StringIO(_decode_text(path)))
    grid: list[list[str]] = []
    for index, row in enumerate(reader, 1):
        if index > 100_000:
            raise DocumentParseError("document_row_limit_exceeded")
        cleaned = [cell.strip() for cell in row]
        if any(cleaned):
            grid.append(cleaned)
    if not grid:
        raise DocumentParseError("document_no_usable_text")
    column_types = {
        header or f"col_{index + 1}": _column_type(
            [row[index] if index < len(row) else "" for row in grid[1:]]
        )
        for index, header in enumerate(grid[0])
    }
    unit = _table_unit(
        grid,
        page=None,
        locator_extra={"source": "csv:1", "rows": f"1-{len(grid)}"},
        attributes={"parse_method": "csv", "column_types": column_types},
    )
    return PreparedDocument(
        units=[unit], page_count=1, parse_method="csv",
    )


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def from_light_text(path: Path, *, markdown: bool) -> PreparedDocument:
    """Light structure for Markdown/TXT: headings plus paragraph windows."""
    text = _decode_text(path).strip()
    if not text:
        raise DocumentParseError("document_no_usable_text")
    units: list[StructuredUnit] = []
    if markdown:
        matches = list(_HEADING_RE.finditer(text))
        if matches:
            bounds = [(match.start(), match.group(2).strip()) for match in matches] + [
                (len(text), None)
            ]
            for (start, heading), (end, _) in zip(bounds, bounds[1:], strict=False):
                body = text[start:end]
                body = _HEADING_RE.sub("", body, count=1).strip()
                if heading or body:
                    units.append(
                        StructuredUnit(
                            unit_type="section_header" if heading else "paragraph",
                            locator={"source": f"section:{len(units) + 1}", "heading": heading},
                            raw_text=body or heading,
                            index_text=f"{heading}\n{body}".strip() if heading else body,
                            attributes={"parse_method": "light_text"},
                        )
                    )
        if units:
            title_match = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
            if title_match:
                units.insert(
                    0,
                    StructuredUnit(
                        unit_type="title",
                        locator={"source": "title:1"},
                        raw_text=title_match.group(1),
                        index_text=title_match.group(1),
                        attributes={"parse_method": "light_text"},
                    ),
                )
    if not units:
        for index, paragraph in enumerate(
            re.split(r"\n\s*\n", text), 1
        ):
            paragraph = paragraph.strip()
            if paragraph:
                units.append(
                    StructuredUnit(
                        unit_type="paragraph",
                        locator={"source": f"paragraph:{index}"},
                        raw_text=paragraph,
                        index_text=paragraph,
                        attributes={"parse_method": "light_text"},
                    )
                )
    return PreparedDocument(units=units, page_count=1, parse_method="light_text")


def plan_segments(prepared: PreparedDocument, settings: DocumentSettings) -> PreparedDocument:
    """Attach segment texts: row groups for tables, windows for prose."""
    segment_texts: list[str] = []
    segment_units: list[int] = []
    for index, unit in enumerate(prepared.units):
        if unit.unit_type == "table":
            groups = plan_table_segments(unit, settings.table_row_group_size)
        else:
            groups = _windows(unit.index_text)
        for text in groups:
            if text:
                segment_texts.append(text)
                segment_units.append(index)
    if not segment_texts:
        raise DocumentParseError("document_no_extractable_text")
    prepared.segment_texts = segment_texts
    prepared.segment_units = segment_units
    return prepared
