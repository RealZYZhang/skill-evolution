#!/usr/bin/env python3
"""Compare preserved HTML artifacts without assigning a quality score."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse


COMPARISON_SCHEMA = "artifact.comparison.v1"
EVIDENCE_SCHEMA = "evidence.ref.v1"
CAMPAIGN_SCHEMA = "replay.campaign.v1"
VIEWPORTS = {
    "desktop": (1440, 900),
    "mobile": (390, 844),
}
VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
LANDMARK_ELEMENTS = {
    "article",
    "aside",
    "footer",
    "form",
    "header",
    "main",
    "nav",
    "section",
}
TEXT_EXCLUDED_ELEMENTS = {
    "script",
    "style",
    "template",
    "noscript",
}
VISIBLE_BLOCK_ELEMENTS = {
    "blockquote",
    "dd",
    "dt",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "pre",
    "summary",
    "td",
    "th",
}
URL_ATTRIBUTES = {
    "audio": "src",
    "embed": "src",
    "iframe": "src",
    "img": "src",
    "input": "src",
    "link": "href",
    "object": "data",
    "script": "src",
    "source": "src",
    "track": "src",
    "video": "src",
}
ARIA_REFERENCE_ATTRIBUTES = {
    "aria-controls",
    "aria-describedby",
    "aria-details",
    "aria-errormessage",
    "aria-flowto",
    "aria-labelledby",
    "aria-owns",
    "for",
    "headers",
    "list",
}
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
WHITESPACE_PATTERN = re.compile(r"\s+")
CSS_VARIABLE_PATTERN = re.compile(
    r"(?P<name>--[A-Za-z0-9_-]+)\s*:\s*(?P<value>[^;{}]+)"
)
CSS_COLOR_PATTERN = re.compile(
    r"#[0-9a-fA-F]{3,8}\b"
    r"|(?:rgb|rgba|hsl|hsla)\([^)]*\)",
    re.IGNORECASE,
)
CSS_COLOR_DECLARATION_PATTERN = re.compile(
    r"(?:^|[;{])\s*"
    r"(?:color|background(?:-color)?|border(?:-[\w-]+)?-color|fill|stroke)"
    r"\s*:\s*(?P<value>[^;{}]+)",
    re.IGNORECASE,
)
CSS_FONT_PATTERN = re.compile(
    r"font-family\s*:\s*(?P<value>[^;{}]+)",
    re.IGNORECASE,
)
CSS_MEDIA_PATTERN = re.compile(
    r"@media\s*(?P<value>[^{]+)\{",
    re.IGNORECASE,
)
CSS_URL_PATTERN = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<url>.*?)"
    r"(?P=quote)\s*\)",
    re.IGNORECASE,
)
CSS_IMPORT_PATTERN = re.compile(
    r"@import\s+(?:url\(\s*)?['\"]?(?P<url>[^'\"\s);]+)",
    re.IGNORECASE,
)
MARKDOWN_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
MARKDOWN_TABLE_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
NUMBER_PATTERN = re.compile(
    r"(?<![\w])[-+]?\d+(?:[.,]\d+)*(?:%|[A-Za-z]+)?"
)


JsonObject = dict[str, Any]


class ArtifactComparisonError(Exception):
    """A concrete input or storage error in artifact comparison."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_text(value: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", value).strip()


def _counter_changes(
    left: Mapping[str, int],
    right: Mapping[str, int],
) -> JsonObject:
    result: JsonObject = {}
    for key in sorted(set(left) | set(right)):
        left_value = int(left.get(key, 0))
        right_value = int(right.get(key, 0))
        if left_value == right_value:
            continue
        result[key] = {
            "left": left_value,
            "right": right_value,
            "delta": right_value - left_value,
        }
    return result


def _set_delta(left: Iterable[str], right: Iterable[str]) -> JsonObject:
    left_set = set(left)
    right_set = set(right)
    return {
        "only_left": sorted(left_set - right_set),
        "only_right": sorted(right_set - left_set),
        "common_count": len(left_set & right_set),
    }


def _is_external_url(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith("//"):
        return True
    parsed = urlparse(stripped)
    return parsed.scheme.lower() in {
        "ftp",
        "http",
        "https",
        "ws",
        "wss",
    }


def _evidence_ref(
    *,
    campaign_id: str | None,
    artifact_id: str,
    run_id: str | None,
    artifact_path: str,
    line: int | None = None,
    selector: str | None = None,
) -> JsonObject:
    reference: JsonObject = {
        "schema": EVIDENCE_SCHEMA,
        "artifact_id": artifact_id,
        "artifact_path": artifact_path,
    }
    if campaign_id is not None:
        reference["campaign_id"] = campaign_id
    if run_id is not None:
        reference["run_id"] = run_id
    if line is not None:
        reference["line"] = line
    if selector is not None:
        reference["selector"] = selector
    return reference


class _Frame:
    """One open HTML element and the text collected below it."""

    def __init__(
        self,
        tag: str,
        selector: str,
        line: int,
        attributes: Mapping[str, str],
        hidden: bool,
    ) -> None:
        self.tag = tag
        self.selector = selector
        self.line = line
        self.attributes = dict(attributes)
        self.hidden = hidden
        self.text_parts: list[str] = []
        self.child_counts: Counter[str] = Counter()


class _HTMLFactParser(HTMLParser):
    """Collect objective HTML structure and content facts."""

    def __init__(
        self,
        *,
        campaign_id: str | None,
        artifact_id: str,
        run_id: str | None,
        artifact_path: str,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.campaign_id = campaign_id
        self.artifact_id = artifact_id
        self.run_id = run_id
        self.artifact_path = artifact_path
        self.stack: list[_Frame] = []
        self.table_stack: list[JsonObject] = []
        self.doctype: str | None = None
        self.title: str | None = None
        self.element_count = 0
        self.max_depth = 0
        self.tag_counts: Counter[str] = Counter()
        self.tag_refs: dict[str, JsonObject] = {}
        self.headings: list[JsonObject] = []
        self.landmarks: list[JsonObject] = []
        self.classes: Counter[str] = Counter()
        self.class_refs: dict[str, JsonObject] = {}
        self.custom_elements: Counter[str] = Counter()
        self.component_attributes: Counter[str] = Counter()
        self.ids: dict[str, list[JsonObject]] = {}
        self.local_links: list[JsonObject] = []
        self.external_links: list[JsonObject] = []
        self.aria_references: list[JsonObject] = []
        self.aria_label_count = 0
        self.tables: list[JsonObject] = []
        self.dependencies: list[JsonObject] = []
        self.css_variables: dict[
            tuple[str, str],
            list[JsonObject],
        ] = {}
        self.css_colors: dict[str, list[JsonObject]] = {}
        self.css_fonts: dict[str, list[JsonObject]] = {}
        self.css_media_queries: dict[str, list[JsonObject]] = {}
        self.inline_script_bytes = 0
        self.inline_script_blocks = 0
        self.inline_scripts: list[JsonObject] = []
        self.external_script_count = 0
        self.visible_text_blocks: list[JsonObject] = []
        self.visible_text_fragments: list[str] = []
        self.parse_issues: list[JsonObject] = []

    def _ref(
        self,
        *,
        line: int,
        selector: str | None = None,
    ) -> JsonObject:
        # Artifact identity lives once on the enclosing artifact record.
        # Repeating it for hundreds of DOM locations would substantially
        # inflate the evidence bundle that later agents must consume.
        reference: JsonObject = {"html_line": line}
        if selector is not None:
            reference["selector"] = selector
        return reference

    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype"):
            self.doctype = decl

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        normalized = tag.lower()
        if normalized not in VOID_ELEMENTS:
            self.handle_endtag(normalized)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        attributes = {
            key.lower(): value if value is not None else ""
            for key, value in attrs
        }
        line, _ = self.getpos()
        if self.stack:
            parent = self.stack[-1]
            parent.child_counts[normalized_tag] += 1
            position = parent.child_counts[normalized_tag]
            parent_selector = parent.selector
        else:
            position = self.tag_counts[normalized_tag] + 1
            parent_selector = ""
        element_id = attributes.get("id")
        if element_id and SAFE_ID_PATTERN.fullmatch(element_id):
            selector = f"#{element_id}"
        else:
            own = f"{normalized_tag}:nth-of-type({position})"
            selector = (
                f"{parent_selector} > {own}"
                if parent_selector
                else own
            )
        inherited_hidden = any(frame.hidden for frame in self.stack)
        own_hidden = (
            "hidden" in attributes
            or attributes.get("aria-hidden", "").lower() == "true"
        )
        frame = _Frame(
            normalized_tag,
            selector,
            line,
            attributes,
            inherited_hidden or own_hidden,
        )

        self.element_count += 1
        self.tag_counts[normalized_tag] += 1
        self.max_depth = max(self.max_depth, len(self.stack) + 1)
        self.tag_refs.setdefault(
            normalized_tag,
            self._ref(line=line, selector=selector),
        )
        reference = self._ref(line=line, selector=selector)

        if (
            normalized_tag in LANDMARK_ELEMENTS
            or attributes.get("role")
        ):
            self.landmarks.append(
                {
                    "tag": normalized_tag,
                    "role": attributes.get("role"),
                    "ref": reference,
                }
            )
        for class_name in attributes.get("class", "").split():
            self.classes[class_name] += 1
            self.class_refs.setdefault(class_name, reference)
        if "-" in normalized_tag:
            self.custom_elements[normalized_tag] += 1
        component = attributes.get("data-component")
        if component:
            self.component_attributes[component] += 1
        if element_id:
            self.ids.setdefault(element_id, []).append(reference)
        if attributes.get("aria-label"):
            self.aria_label_count += 1
        for attribute in ARIA_REFERENCE_ATTRIBUTES:
            value = attributes.get(attribute)
            if not value:
                continue
            for target in value.split():
                self.aria_references.append(
                    {
                        "attribute": attribute,
                        "target": target,
                        "resolved": False,
                        "ref": reference,
                    }
                )

        href = attributes.get("href")
        if normalized_tag == "a" and href:
            if href.startswith("#"):
                self.local_links.append(
                    {
                        "target": href[1:],
                        "resolved": False,
                        "ref": reference,
                    }
                )
            elif _is_external_url(href):
                self.external_links.append(
                    {"url": href, "ref": reference}
                )

        dependency_attribute = URL_ATTRIBUTES.get(normalized_tag)
        if dependency_attribute:
            value = attributes.get(dependency_attribute)
            if value and _is_external_url(value):
                dependency = {
                    "kind": f"{normalized_tag}.{dependency_attribute}",
                    "url": value,
                    "ref": reference,
                }
                self.dependencies.append(dependency)
                if normalized_tag == "script":
                    self.external_script_count += 1

        style = attributes.get("style")
        if style:
            self._inspect_css(style, line, selector)

        if normalized_tag == "table":
            self.table_stack.append(
                {
                    "selector": selector,
                    "ref": reference,
                    "row_count": 0,
                    "header_cells": [],
                }
            )
        elif normalized_tag == "tr" and self.table_stack:
            self.table_stack[-1]["row_count"] += 1

        if normalized_tag not in VOID_ELEMENTS:
            self.stack.append(frame)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        matching_index = next(
            (
                index
                for index in range(len(self.stack) - 1, -1, -1)
                if self.stack[index].tag == normalized_tag
            ),
            None,
        )
        if matching_index is None:
            line, _ = self.getpos()
            self.parse_issues.append(
                {
                    "code": "unmatched_end_tag",
                    "message": f"Unmatched closing tag </{normalized_tag}>.",
                    "ref": self._ref(line=line),
                }
            )
            return
        while len(self.stack) - 1 > matching_index:
            dangling = self.stack.pop()
            self._finalize_frame(dangling)
            self.parse_issues.append(
                {
                    "code": "implicitly_closed_tag",
                    "message": (
                        f"<{dangling.tag}> was implicitly closed before "
                        f"</{normalized_tag}>."
                    ),
                    "ref": self._ref(
                        line=dangling.line,
                        selector=dangling.selector,
                    ),
                }
            )
        frame = self.stack.pop()
        self._finalize_frame(frame)

    def handle_data(self, data: str) -> None:
        for frame in self.stack:
            frame.text_parts.append(data)
        if not data:
            return
        line, _ = self.getpos()
        if self.stack and self.stack[-1].tag == "style":
            self._inspect_css(
                data,
                line,
                self.stack[-1].selector,
            )
        if self.stack and self.stack[-1].tag == "script":
            script_bytes = len(data.encode("utf-8"))
            self.inline_script_bytes += script_bytes
            if data.strip():
                self.inline_script_blocks += 1
                self.inline_scripts.append(
                    {
                        "bytes": script_bytes,
                        "ref": self._ref(
                            line=line,
                            selector=self.stack[-1].selector,
                        ),
                    }
                )
        if any(
            frame.hidden or frame.tag in TEXT_EXCLUDED_ELEMENTS
            for frame in self.stack
        ):
            return
        normalized = _normalize_text(data)
        if normalized:
            self.visible_text_fragments.append(normalized)

    def close(self) -> None:
        super().close()
        while self.stack:
            frame = self.stack.pop()
            self._finalize_frame(frame)
            self.parse_issues.append(
                {
                    "code": "unclosed_tag",
                    "message": f"Unclosed tag <{frame.tag}>.",
                    "ref": self._ref(
                        line=frame.line,
                        selector=frame.selector,
                    ),
                }
            )
        targets = set(self.ids)
        for link in self.local_links:
            link["resolved"] = link["target"] in targets
        for reference in self.aria_references:
            reference["resolved"] = reference["target"] in targets

    def _finalize_frame(self, frame: _Frame) -> None:
        text = _normalize_text("".join(frame.text_parts))
        if frame.tag == "title" and self.title is None:
            self.title = text
        if re.fullmatch(r"h[1-6]", frame.tag):
            self.headings.append(
                {
                    "level": int(frame.tag[1]),
                    "text": text,
                    "ref": self._ref(
                        line=frame.line,
                        selector=frame.selector,
                    ),
                }
            )
        if frame.tag == "th" and self.table_stack:
            self.table_stack[-1]["header_cells"].append(text)
        if frame.tag == "table" and self.table_stack:
            table = self.table_stack.pop()
            table["text"] = text
            self.tables.append(table)
        if (
            frame.tag in VISIBLE_BLOCK_ELEMENTS
            and not frame.hidden
            and text
        ):
            self.visible_text_blocks.append(
                {
                    "text": text,
                    "ref": self._ref(
                        line=frame.line,
                        selector=frame.selector,
                    ),
                }
            )

    def _inspect_css(
        self,
        css: str,
        base_line: int,
        selector: str,
    ) -> None:
        def line_for(position: int) -> int:
            return base_line + css[:position].count("\n")

        def add(
            collection: dict[str, list[JsonObject]],
            key: str,
            position: int,
        ) -> None:
            normalized = _normalize_text(key)
            if not normalized:
                return
            collection.setdefault(normalized, []).append(
                self._ref(
                    line=line_for(position),
                    selector=selector,
                )
            )

        for match in CSS_VARIABLE_PATTERN.finditer(css):
            variable_key = (
                match.group("name"),
                _normalize_text(match.group("value")),
            )
            self.css_variables.setdefault(variable_key, []).append(
                self._ref(
                    line=line_for(match.start()),
                    selector=selector,
                )
            )
        for match in CSS_COLOR_PATTERN.finditer(css):
            add(self.css_colors, match.group(0).lower(), match.start())
        for match in CSS_COLOR_DECLARATION_PATTERN.finditer(css):
            value = match.group("value").strip()
            if value and not value.startswith("var("):
                add(self.css_colors, value.lower(), match.start())
        for match in CSS_FONT_PATTERN.finditer(css):
            add(self.css_fonts, match.group("value"), match.start())
        for match in CSS_MEDIA_PATTERN.finditer(css):
            add(
                self.css_media_queries,
                match.group("value"),
                match.start(),
            )
        for pattern in (CSS_URL_PATTERN, CSS_IMPORT_PATTERN):
            for match in pattern.finditer(css):
                value = match.group("url").strip()
                if _is_external_url(value):
                    self.dependencies.append(
                        {
                            "kind": "css.url",
                            "url": value,
                            "ref": self._ref(
                                line=line_for(match.start()),
                                selector=selector,
                            ),
                        }
                    )

    def facts(self, byte_count: int) -> JsonObject:
        """Return JSON-ready facts after parsing has completed."""

        duplicate_ids = [
            {
                "id": element_id,
                "count": len(references),
                "refs": references,
            }
            for element_id, references in sorted(self.ids.items())
            if len(references) > 1
        ]
        unresolved_anchors = [
            link for link in self.local_links if not link["resolved"]
        ]
        unresolved_aria = [
            reference
            for reference in self.aria_references
            if not reference["resolved"]
        ]
        visible_text = _normalize_text(
            " ".join(self.visible_text_fragments)
        )
        return {
            "document": {
                "bytes": byte_count,
                "doctype": self.doctype,
                "title": self.title,
                "element_count": self.element_count,
                "max_depth": self.max_depth,
                "has_html": self.tag_counts["html"] > 0,
                "has_head": self.tag_counts["head"] > 0,
                "has_body": self.tag_counts["body"] > 0,
            },
            "structure": {
                "tag_counts": dict(sorted(self.tag_counts.items())),
                "tag_refs": dict(sorted(self.tag_refs.items())),
                "headings": self.headings,
                "landmarks": self.landmarks,
                "classes": [
                    {
                        "name": name,
                        "count": count,
                        "ref": self.class_refs[name],
                    }
                    for name, count in sorted(self.classes.items())
                ],
                "components": {
                    "custom_elements": dict(
                        sorted(self.custom_elements.items())
                    ),
                    "data_components": dict(
                        sorted(self.component_attributes.items())
                    ),
                },
                "tables": self.tables,
            },
            "links": {
                "id_count": len(self.ids),
                "duplicate_ids": duplicate_ids,
                "local_anchors": self.local_links,
                "unresolved_local_anchors": unresolved_anchors,
                "external_links": self.external_links,
            },
            "accessibility": {
                "aria_label_count": self.aria_label_count,
                "id_references": self.aria_references,
                "unresolved_id_references": unresolved_aria,
            },
            "dependencies": {
                "external": self.dependencies,
                "external_count": len(self.dependencies),
            },
            "css": {
                "variables": _variable_occurrences(
                    self.css_variables
                ),
                "colors": _occurrences(self.css_colors),
                "fonts": _occurrences(self.css_fonts),
                "media_queries": _occurrences(
                    self.css_media_queries
                ),
            },
            "scripts": {
                "inline_blocks": self.inline_script_blocks,
                "inline_bytes": self.inline_script_bytes,
                "blocks": self.inline_scripts,
                "external_count": self.external_script_count,
            },
            "text": {
                "normalized": visible_text,
                "characters": len(visible_text),
                "words": len(visible_text.split()),
                "blocks": self.visible_text_blocks,
                "block_count": len(self.visible_text_blocks),
            },
            "parse_issues": self.parse_issues,
        }


def _occurrences(
    values: Mapping[str, Sequence[JsonObject]],
) -> list[JsonObject]:
    return [
        {
            "value": value,
            "count": len(references),
            "refs": list(references),
        }
        for value, references in sorted(values.items())
    ]


def _variable_occurrences(
    values: Mapping[tuple[str, str], Sequence[JsonObject]],
) -> list[JsonObject]:
    return [
        {
            "name": name,
            "value": value,
            "count": len(references),
            "refs": list(references),
        }
        for (name, value), references in sorted(values.items())
    ]


def _strip_markdown(value: str) -> str:
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", value)
    value = re.sub(r"^\s*>\s?", "", value)
    value = re.sub(r"[`*_~]", "", value)
    return _normalize_text(value)


def _markdown_cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [_strip_markdown(cell) for cell in stripped.split("|")]


def _markdown_facts(path: Path, text: str) -> JsonObject:
    lines = text.splitlines()
    headings: list[JsonObject] = []
    tables: list[JsonObject] = []
    blocks: list[JsonObject] = []
    fenced = False
    current_block: list[str] = []
    current_start: int | None = None

    def flush_block() -> None:
        nonlocal current_block, current_start
        if not current_block or current_start is None:
            current_block = []
            current_start = None
            return
        normalized = _strip_markdown(" ".join(current_block))
        if normalized:
            blocks.append(
                {
                    "text": normalized,
                    "source_line": current_start,
                }
            )
        current_block = []
        current_start = None

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush_block()
            fenced = not fenced
            index += 1
            continue
        if fenced:
            index += 1
            continue
        heading_match = MARKDOWN_HEADING_PATTERN.match(line)
        if heading_match:
            flush_block()
            headings.append(
                {
                    "level": len(heading_match.group(1)),
                    "text": _strip_markdown(heading_match.group(2)),
                    "source_line": index + 1,
                }
            )
            index += 1
            continue
        if (
            index + 1 < len(lines)
            and "|" in line
            and MARKDOWN_TABLE_SEPARATOR.match(lines[index + 1])
        ):
            flush_block()
            start = index + 1
            headers = _markdown_cells(line)
            row_count = 1
            index += 2
            while index < len(lines) and "|" in lines[index]:
                if not lines[index].strip():
                    break
                row_count += 1
                index += 1
            tables.append(
                {
                    "source_line": start,
                    "header_cells": headers,
                    "row_count": row_count,
                }
            )
            continue
        if not stripped:
            flush_block()
        else:
            if current_start is None:
                current_start = index + 1
            current_block.append(line)
        index += 1
    flush_block()
    urls = sorted(
        {
            match.group(0).rstrip(".,;:!?")
            for match in URL_PATTERN.finditer(text)
        }
    )
    numbers = Counter(match.group(0) for match in NUMBER_PATTERN.finditer(text))
    return {
        "kind": "markdown",
        "path": str(path),
        "headings": headings,
        "numbers": dict(sorted(numbers.items())),
        "urls": urls,
        "tables": tables,
        "text_blocks": blocks,
    }


def _heading_subsequence(
    expected: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> tuple[int, list[JsonObject]]:
    cursor = 0
    matched = 0
    missing: list[JsonObject] = []
    observed_keys = [
        (item.get("level"), _normalize_text(str(item.get("text", ""))))
        for item in observed
    ]
    for item in expected:
        key = (
            item.get("level"),
            _normalize_text(str(item.get("text", ""))),
        )
        found = next(
            (
                index
                for index in range(cursor, len(observed_keys))
                if observed_keys[index] == key
            ),
            None,
        )
        if found is None:
            missing.append(dict(item))
        else:
            matched += 1
            cursor = found + 1
    return matched, missing


def _source_preservation(
    source: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> JsonObject:
    if source.get("kind") != "markdown":
        return {
            "status": "unsupported_source_kind",
            "source_kind": source.get("kind"),
        }
    structure = facts["structure"]
    text = facts["text"]["normalized"]
    headings = source["headings"]
    matched_headings, missing_headings = _heading_subsequence(
        headings,
        structure["headings"],
    )
    observed_numbers = Counter(
        match.group(0) for match in NUMBER_PATTERN.finditer(text)
    )
    missing_numbers: JsonObject = {}
    preserved_number_count = 0
    expected_number_count = 0
    for value, count in source["numbers"].items():
        expected_number_count += count
        preserved = min(count, observed_numbers[value])
        preserved_number_count += preserved
        if preserved < count:
            missing_numbers[value] = count - preserved
    output_urls = {
        item["url"] for item in facts["links"]["external_links"]
    } | {
        item["url"] for item in facts["dependencies"]["external"]
    } | {
        match.group(0).rstrip(".,;:!?")
        for match in URL_PATTERN.finditer(text)
    }
    missing_urls = [
        value for value in source["urls"] if value not in output_urls
    ]
    output_tables = structure["tables"]
    table_results: list[JsonObject] = []
    for table in source["tables"]:
        headers = table["header_cells"]
        match = next(
            (
                output
                for output in output_tables
                if all(
                    header in output["header_cells"]
                    for header in headers
                )
            ),
            None,
        )
        table_results.append(
            {
                "source_line": table["source_line"],
                "header_cells": headers,
                "header_cells_preserved": match is not None,
            }
        )
    missing_blocks = [
        block
        for block in source["text_blocks"]
        if block["text"] not in text
    ]
    return {
        "status": "measured",
        "headings": {
            "expected": len(headings),
            "preserved_in_order": matched_headings,
            "missing_or_reordered": missing_headings,
        },
        "numbers": {
            "expected_occurrences": expected_number_count,
            "preserved_occurrences": preserved_number_count,
            "missing_occurrences": missing_numbers,
        },
        "urls": {
            "expected": len(source["urls"]),
            "preserved": len(source["urls"]) - len(missing_urls),
            "missing": missing_urls,
        },
        "tables": {
            "expected": len(source["tables"]),
            "output": len(output_tables),
            "header_results": table_results,
        },
        "text_blocks": {
            "expected": len(source["text_blocks"]),
            "preserved_exactly": (
                len(source["text_blocks"]) - len(missing_blocks)
            ),
            "missing": missing_blocks,
        },
    }


def _scalar_delta(
    left: int | float,
    right: int | float,
) -> JsonObject:
    return {
        "left": left,
        "right": right,
        "delta": right - left,
    }


def _values(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        (
            f"{entry['name']}={entry['value']}"
            if "name" in entry
            else str(entry["value"])
        )
        for entry in entries
    ]


def _pairwise_delta(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> JsonObject:
    left_facts = left["facts"]
    right_facts = right["facts"]
    left_structure = left_facts["structure"]
    right_structure = right_facts["structure"]
    left_text_blocks = {
        item["text"] for item in left_facts["text"]["blocks"]
    }
    right_text_blocks = {
        item["text"] for item in right_facts["text"]["blocks"]
    }
    union = left_text_blocks | right_text_blocks
    intersection = left_text_blocks & right_text_blocks
    left_heading_outline = [
        [item["level"], item["text"]]
        for item in left_structure["headings"]
    ]
    right_heading_outline = [
        [item["level"], item["text"]]
        for item in right_structure["headings"]
    ]
    common_prefix = 0
    for left_heading, right_heading in zip(
        left_heading_outline,
        right_heading_outline,
    ):
        if left_heading != right_heading:
            break
        common_prefix += 1
    left_landmarks = Counter(
        item["role"] or item["tag"]
        for item in left_structure["landmarks"]
    )
    right_landmarks = Counter(
        item["role"] or item["tag"]
        for item in right_structure["landmarks"]
    )
    left_classes = {
        item["name"] for item in left_structure["classes"]
    }
    right_classes = {
        item["name"] for item in right_structure["classes"]
    }
    left_preservation = left.get("source_preservation", {})
    right_preservation = right.get("source_preservation", {})
    preservation_delta: JsonObject = {}
    if (
        left_preservation.get("status") == "measured"
        and right_preservation.get("status") == "measured"
    ):
        for category, key in (
            ("headings", "preserved_in_order"),
            ("numbers", "preserved_occurrences"),
            ("urls", "preserved"),
            ("text_blocks", "preserved_exactly"),
        ):
            preservation_delta[category] = _scalar_delta(
                left_preservation[category][key],
                right_preservation[category][key],
            )
    return {
        "document": {
            "bytes": _scalar_delta(
                left_facts["document"]["bytes"],
                right_facts["document"]["bytes"],
            ),
            "element_count": _scalar_delta(
                left_facts["document"]["element_count"],
                right_facts["document"]["element_count"],
            ),
            "max_depth": _scalar_delta(
                left_facts["document"]["max_depth"],
                right_facts["document"]["max_depth"],
            ),
        },
        "structure": {
            "tag_count_changes": _counter_changes(
                left_structure["tag_counts"],
                right_structure["tag_counts"],
            ),
            "landmark_count_changes": _counter_changes(
                left_landmarks,
                right_landmarks,
            ),
            "classes": _set_delta(left_classes, right_classes),
            "heading_outline": {
                "exactly_equal": (
                    left_heading_outline == right_heading_outline
                ),
                "common_prefix_length": common_prefix,
                "left": left_heading_outline,
                "right": right_heading_outline,
            },
            "table_count": _scalar_delta(
                len(left_structure["tables"]),
                len(right_structure["tables"]),
            ),
        },
        "links_and_accessibility": {
            "local_anchor_count": _scalar_delta(
                len(left_facts["links"]["local_anchors"]),
                len(right_facts["links"]["local_anchors"]),
            ),
            "unresolved_anchor_count": _scalar_delta(
                len(left_facts["links"]["unresolved_local_anchors"]),
                len(right_facts["links"]["unresolved_local_anchors"]),
            ),
            "unresolved_aria_reference_count": _scalar_delta(
                len(
                    left_facts["accessibility"][
                        "unresolved_id_references"
                    ]
                ),
                len(
                    right_facts["accessibility"][
                        "unresolved_id_references"
                    ]
                ),
            ),
        },
        "dependencies": {
            "external_count": _scalar_delta(
                left_facts["dependencies"]["external_count"],
                right_facts["dependencies"]["external_count"],
            )
        },
        "css": {
            name: _set_delta(
                _values(left_facts["css"][name]),
                _values(right_facts["css"][name]),
            )
            for name in (
                "variables",
                "colors",
                "fonts",
                "media_queries",
            )
        },
        "scripts": {
            "inline_bytes": _scalar_delta(
                left_facts["scripts"]["inline_bytes"],
                right_facts["scripts"]["inline_bytes"],
            ),
            "external_count": _scalar_delta(
                left_facts["scripts"]["external_count"],
                right_facts["scripts"]["external_count"],
            ),
        },
        "text": {
            "characters": _scalar_delta(
                left_facts["text"]["characters"],
                right_facts["text"]["characters"],
            ),
            "words": _scalar_delta(
                left_facts["text"]["words"],
                right_facts["text"]["words"],
            ),
            "block_sets": {
                "only_left_count": len(
                    left_text_blocks - right_text_blocks
                ),
                "only_right_count": len(
                    right_text_blocks - left_text_blocks
                ),
                "common_count": len(intersection),
                "jaccard": (
                    len(intersection) / len(union) if union else 1.0
                ),
            },
        },
        "source_preservation": preservation_delta,
    }


def _inject_screenshot_guards(html: str) -> str:
    """Return a temporary render copy with network and motion guards."""

    guard = (
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; img-src data: blob:; '
        "style-src 'unsafe-inline'; font-src data:; "
        "script-src 'nonce-skill-evolution-probe'; "
        "connect-src 'none'; frame-src 'none'; object-src 'none'; "
        'base-uri \'none\'; form-action \'none\'">'
        "<style>*{animation:none!important;transition:none!important}"
        "html{scroll-behavior:auto!important}</style>"
        '<script nonce="skill-evolution-probe">'
        "document.documentElement.dataset.skillEvolutionCapture='true';"
        "</script>"
    )
    head_match = re.search(r"<head(?:\s[^>]*)?>", html, re.IGNORECASE)
    if head_match:
        return html[: head_match.end()] + guard + html[head_match.end() :]
    doctype_match = re.match(
        r"\s*<!doctype[^>]*>",
        html,
        re.IGNORECASE,
    )
    if doctype_match:
        return (
            html[: doctype_match.end()]
            + "<head>"
            + guard
            + "</head>"
            + html[doctype_match.end() :]
        )
    return "<head>" + guard + "</head>" + html


def _find_chrome(explicit: str | None) -> str | None:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        return shutil.which(explicit)
    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        (
            "/Applications/Google Chrome.app/Contents/MacOS/"
            "Google Chrome"
        ),
        (
            "/Applications/Chromium.app/Contents/MacOS/"
            "Chromium"
        ),
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.is_absolute() and path.is_file():
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _safe_filename(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return normalized or "artifact"


def _is_complete_png(path: Path) -> bool:
    try:
        if path.stat().st_size < 20:
            return False
        with path.open("rb") as stream:
            signature = stream.read(8)
            stream.seek(-12, 2)
            ending = stream.read(12)
    except OSError:
        return False
    return (
        signature == b"\x89PNG\r\n\x1a\n"
        and ending == b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _capture_viewport(
    *,
    command: Sequence[str],
    destination: Path,
    timeout: float,
) -> tuple[bool, str | None]:
    """Wait for a complete PNG, not for a lingering Chrome process."""

    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        return False, type(error).__name__
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if _is_complete_png(destination):
                return True, None
            return_code = process.poll()
            if return_code is not None:
                # Give the filesystem one final chance to expose a flushed
                # screenshot after the browser process has exited.
                return _is_complete_png(destination), (
                    None
                    if _is_complete_png(destination)
                    else f"exit_status_{return_code}"
                )
            time.sleep(0.05)
        if _is_complete_png(destination):
            return True, None
        return False, "timeout"
    finally:
        _stop_process(process)


def _capture_screenshots(
    *,
    chrome: str,
    artifact_id: str,
    html: str,
    output_directory: Path,
    timeout: float,
) -> tuple[JsonObject, list[JsonObject]]:
    screenshots: JsonObject = {}
    issues: list[JsonObject] = []
    output_directory.mkdir(parents=True, exist_ok=True)
    relative_directory = Path(output_directory.name)
    with tempfile.TemporaryDirectory(
        prefix="skill-evolution-artifact-"
    ) as temporary:
        temporary_directory = Path(temporary)
        protected = temporary_directory / "protected.html"
        protected.write_text(
            _inject_screenshot_guards(html),
            encoding="utf-8",
        )
        user_data = temporary_directory / "chrome-profile"
        for viewport, (width, height) in VIEWPORTS.items():
            destination = (
                output_directory
                / f"{_safe_filename(artifact_id)}-{viewport}.png"
            ).resolve()
            portable_path = str(
                relative_directory / destination.name
            )
            try:
                destination.unlink(missing_ok=True)
            except OSError as error:
                issues.append(
                    {
                        "code": "screenshot_destination_unwritable",
                        "message": (
                            f"{viewport} screenshot destination could not "
                            f"be replaced: {type(error).__name__}."
                        ),
                        "artifact_id": artifact_id,
                    }
                )
                screenshots[viewport] = {
                    "status": "failed",
                    "width": width,
                    "height": height,
                    "path": portable_path,
                }
                continue
            command = [
                chrome,
                "--headless=new",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-gpu",
                "--disable-sync",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                "--force-device-scale-factor=1",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=1000",
                "--host-resolver-rules=MAP * 0.0.0.0",
                f"--user-data-dir={user_data}",
                f"--window-size={width},{height}",
                f"--screenshot={destination}",
                protected.as_uri(),
            ]
            captured, failure = _capture_viewport(
                command=command,
                destination=destination,
                timeout=timeout,
            )
            if not captured:
                issues.append(
                    {
                        "code": "chrome_capture_failed",
                        "message": (
                            f"{viewport} screenshot failed: {failure}."
                        ),
                        "artifact_id": artifact_id,
                    }
                )
                screenshots[viewport] = {
                    "status": "failed",
                    "width": width,
                    "height": height,
                    "path": portable_path,
                }
                continue
            screenshots[viewport] = {
                "status": "captured",
                "width": width,
                "height": height,
                "path": portable_path,
                "bytes": destination.stat().st_size,
            }
    return screenshots, issues


class HTMLArtifactComparator:
    """Create objective, evidence-linked comparisons of HTML artifacts."""

    def __init__(
        self,
        *,
        chrome_command: str | None = None,
        screenshot_timeout: float = 30.0,
    ) -> None:
        if screenshot_timeout <= 0:
            raise ValueError("screenshot_timeout must be greater than zero")
        self.chrome_command = chrome_command
        self.screenshot_timeout = screenshot_timeout

    def compare(
        self,
        artifacts: Mapping[str, str | Path],
        *,
        source_path: str | Path | None = None,
        campaign_id: str | None = None,
        run_ids: Mapping[str, str] | None = None,
        display_paths: Mapping[str, str] | None = None,
        comparison_groups: Mapping[str, str] | None = None,
        capture_screenshots: bool = False,
        screenshot_directory: str | Path | None = None,
    ) -> JsonObject:
        """Compare explicit artifact paths and return the public schema."""

        if not artifacts:
            raise ValueError("at least one HTML artifact is required")
        source = self._load_source(source_path)
        chrome = (
            _find_chrome(self.chrome_command)
            if capture_screenshots
            else None
        )
        if capture_screenshots and screenshot_directory is None:
            raise ValueError(
                "screenshot_directory is required when screenshots are enabled"
            )
        issues: list[JsonObject] = []
        if capture_screenshots and chrome is None:
            issues.append(
                {
                    "code": "chrome_not_found",
                    "message": (
                        "Chrome is unavailable; static comparison completed "
                        "without screenshots."
                    ),
                }
            )

        artifact_results: list[JsonObject] = []
        for artifact_id, raw_path in sorted(artifacts.items()):
            path = Path(raw_path).resolve()
            if not path.is_file():
                artifact_results.append(
                    {
                        "artifact_id": artifact_id,
                        "run_id": (run_ids or {}).get(artifact_id),
                        "artifact_path": str(raw_path),
                        "comparison_group": (
                            comparison_groups or {}
                        ).get(artifact_id, "default"),
                        "status": "error",
                        "facts": None,
                        "source_preservation": None,
                        "screenshots": {},
                        "issues": [
                            {
                                "code": "artifact_missing",
                                "message": (
                                    f"Artifact does not exist: {raw_path}"
                                ),
                            }
                        ],
                    }
                )
                continue
            try:
                html = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                artifact_results.append(
                    {
                        "artifact_id": artifact_id,
                        "run_id": (run_ids or {}).get(artifact_id),
                        "artifact_path": str(raw_path),
                        "comparison_group": (
                            comparison_groups or {}
                        ).get(artifact_id, "default"),
                        "status": "error",
                        "facts": None,
                        "source_preservation": None,
                        "screenshots": {},
                        "issues": [
                            {
                                "code": "artifact_unreadable",
                                "message": str(error),
                            }
                        ],
                    }
                )
                continue
            artifact_path = (display_paths or {}).get(
                artifact_id,
                str(path),
            )
            run_id = (run_ids or {}).get(artifact_id)
            parser = _HTMLFactParser(
                campaign_id=campaign_id,
                artifact_id=artifact_id,
                run_id=run_id,
                artifact_path=artifact_path,
            )
            artifact_issues: list[JsonObject] = []
            try:
                parser.feed(html)
                parser.close()
                facts = parser.facts(len(html.encode("utf-8")))
            except Exception as error:
                artifact_results.append(
                    {
                        "artifact_id": artifact_id,
                        "run_id": run_id,
                        "artifact_path": artifact_path,
                        "comparison_group": (
                            comparison_groups or {}
                        ).get(artifact_id, "default"),
                        "status": "error",
                        "facts": None,
                        "source_preservation": None,
                        "screenshots": {},
                        "issues": [
                            {
                                "code": "html_parse_failed",
                                "message": (
                                    f"{type(error).__name__}: {error}"
                                ),
                            }
                        ],
                    }
                )
                continue
            screenshots: JsonObject = {}
            if capture_screenshots and chrome is not None:
                screenshots, screenshot_issues = _capture_screenshots(
                    chrome=chrome,
                    artifact_id=artifact_id,
                    html=html,
                    output_directory=Path(screenshot_directory),
                    timeout=self.screenshot_timeout,
                )
                artifact_issues.extend(screenshot_issues)
            preservation = (
                _source_preservation(source, facts)
                if source is not None
                else {"status": "not_requested"}
            )
            artifact_results.append(
                {
                    "artifact_id": artifact_id,
                    "run_id": run_id,
                    "artifact_path": artifact_path,
                    "comparison_group": (
                        comparison_groups or {}
                    ).get(artifact_id, "default"),
                    "status": (
                        "partial" if artifact_issues else "complete"
                    ),
                    "facts": facts,
                    "source_preservation": preservation,
                    "screenshots": screenshots,
                    "issues": artifact_issues,
                    "evidence_ref": _evidence_ref(
                        campaign_id=campaign_id,
                        artifact_id=artifact_id,
                        run_id=run_id,
                        artifact_path=artifact_path,
                    ),
                }
            )

        comparable = [
            artifact
            for artifact in artifact_results
            if artifact["facts"] is not None
        ]
        pairwise: list[JsonObject] = []
        for left_index, left in enumerate(comparable):
            for right in comparable[left_index + 1 :]:
                if (
                    left["comparison_group"]
                    != right["comparison_group"]
                ):
                    continue
                pairwise.append(
                    {
                        "left_artifact_id": left["artifact_id"],
                        "right_artifact_id": right["artifact_id"],
                        "delta": _pairwise_delta(left, right),
                        "evidence_refs": [
                            left["evidence_ref"],
                            right["evidence_ref"],
                        ],
                    }
                )
        error_count = sum(
            artifact["status"] == "error"
            for artifact in artifact_results
        )
        partial_count = sum(
            artifact["status"] == "partial"
            for artifact in artifact_results
        )
        if error_count == len(artifact_results):
            status = "error"
        elif error_count or partial_count or issues:
            status = "partial"
        else:
            status = "complete"
        return {
            "schema": COMPARISON_SCHEMA,
            "generated_at": _utc_now(),
            "status": status,
            "campaign_id": campaign_id,
            "source": source,
            "artifacts": artifact_results,
            "pairwise": pairwise,
            "issues": issues,
        }

    def compare_campaign(
        self,
        campaign_directory: str | Path,
        *,
        source_path: str | Path | None = None,
        capture_screenshots: bool = False,
        screenshot_directory: str | Path | None = None,
    ) -> JsonObject:
        """Resolve declared campaign artifacts without leaving the campaign."""

        campaign = Path(campaign_directory).resolve()
        manifest_path = campaign / "replay.json"
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ArtifactComparisonError(
                f"Unable to read campaign manifest: {error}"
            ) from error
        if not isinstance(manifest, dict):
            raise ArtifactComparisonError(
                "Campaign manifest must contain a JSON object"
            )
        if manifest.get("schema") != CAMPAIGN_SCHEMA:
            raise ArtifactComparisonError(
                f"Unsupported campaign schema: {manifest.get('schema')!r}"
            )
        campaign_id = manifest.get("campaign_id")
        if not isinstance(campaign_id, str) or not campaign_id:
            campaign_id = campaign.name
        artifacts: dict[str, Path] = {}
        display_paths: dict[str, str] = {}
        run_ids: dict[str, str] = {}
        comparison_groups: dict[str, str] = {}
        resolution_issues: list[JsonObject] = []
        runs = manifest.get("runs")
        if not isinstance(runs, list):
            raise ArtifactComparisonError(
                "Campaign manifest field 'runs' must be a list"
            )
        for position, run in enumerate(runs, start=1):
            if not isinstance(run, Mapping):
                resolution_issues.append(
                    {
                        "code": "invalid_run_record",
                        "message": f"Run entry {position} is not an object.",
                    }
                )
                continue
            run_id = run.get("run_id")
            relative_run = run.get("path")
            if (
                not isinstance(run_id, str)
                or not isinstance(relative_run, str)
            ):
                resolution_issues.append(
                    {
                        "code": "artifact_reference_missing",
                        "message": (
                            f"Run entry {position} has no usable path or ID."
                        ),
                    }
                )
                continue
            artifact_values = run.get("artifacts")
            declared: list[Mapping[str, Any]] = []
            if isinstance(artifact_values, list):
                declared.extend(
                    value
                    for value in artifact_values
                    if isinstance(value, Mapping)
                )
            legacy_artifact = run.get("artifact")
            if not declared and isinstance(legacy_artifact, Mapping):
                declared.append(legacy_artifact)
            if not declared:
                resolution_issues.append(
                    {
                        "code": "artifact_reference_missing",
                        "message": (
                            f"Run '{run_id}' has no declared artifacts."
                        ),
                    }
                )
                continue
            for artifact in declared:
                relative_artifact = artifact.get("path")
                if not isinstance(relative_artifact, str):
                    resolution_issues.append(
                        {
                            "code": "artifact_reference_missing",
                            "message": (
                                f"Run '{run_id}' has an artifact without "
                                "a path."
                            ),
                        }
                    )
                    continue
                candidate = (
                    campaign / relative_run / relative_artifact
                ).resolve()
                if not candidate.is_relative_to(campaign):
                    resolution_issues.append(
                        {
                            "code": "artifact_outside_campaign",
                            "message": (
                                f"Run '{run_id}' artifact leaves the "
                                "campaign."
                            ),
                        }
                    )
                    continue
                if candidate.suffix.lower() not in {".htm", ".html"}:
                    resolution_issues.append(
                        {
                            "code": "artifact_not_html",
                            "severity": "info",
                            "message": (
                                f"Run '{run_id}' non-HTML artifact was "
                                "not compared: {relative_artifact}"
                            ),
                        }
                    )
                    continue
                normalized_group = Path(relative_artifact).as_posix()
                if normalized_group.startswith("artifacts/"):
                    normalized_group = normalized_group[
                        len("artifacts/") :
                    ]
                artifact_id = (
                    run_id
                    if len(declared) == 1
                    else f"{run_id}:{normalized_group}"
                )
                relative_display = str(candidate.relative_to(campaign))
                artifacts[artifact_id] = candidate
                display_paths[artifact_id] = relative_display
                run_ids[artifact_id] = run_id
                comparison_groups[artifact_id] = normalized_group

        resolved_source = source_path
        if resolved_source is None:
            resolved_source = self._campaign_source(campaign, manifest)
        if not artifacts:
            result: JsonObject = {
                "schema": COMPARISON_SCHEMA,
                "generated_at": _utc_now(),
                "status": "error",
                "campaign_id": campaign_id,
                "source": self._load_source(resolved_source),
                "artifacts": [],
                "pairwise": [],
                "issues": resolution_issues,
            }
            return result
        result = self.compare(
            artifacts,
            source_path=resolved_source,
            campaign_id=campaign_id,
            run_ids=run_ids,
            display_paths=display_paths,
            comparison_groups=comparison_groups,
            capture_screenshots=capture_screenshots,
            screenshot_directory=screenshot_directory,
        )
        result["issues"] = [*resolution_issues, *result["issues"]]
        actionable_resolution_issues = [
            issue
            for issue in resolution_issues
            if issue.get("severity") != "info"
        ]
        if (
            actionable_resolution_issues
            and result["status"] == "complete"
        ):
            result["status"] = "partial"
        return result

    def _load_source(
        self,
        source_path: str | Path | None,
    ) -> JsonObject | None:
        if source_path is None:
            return None
        path = Path(source_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Source does not exist: {path}")
        if path.suffix.lower() not in {".md", ".markdown"}:
            return {
                "kind": path.suffix.lower().lstrip(".") or "unknown",
                "path": str(path),
                "status": "unsupported_for_preservation_extraction",
            }
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ArtifactComparisonError(
                f"Unable to read source document: {error}"
            ) from error
        facts = _markdown_facts(path, text)
        facts["status"] = "extracted"
        return facts

    def _campaign_source(
        self,
        campaign: Path,
        manifest: Mapping[str, Any],
    ) -> Path | None:
        runs = manifest.get("runs")
        if isinstance(runs, list):
            for run in runs:
                if not isinstance(run, Mapping):
                    continue
                relative_run = run.get("path")
                if not isinstance(relative_run, str):
                    continue
                run_directory = (campaign / relative_run).resolve()
                if not run_directory.is_relative_to(campaign):
                    continue
                for name in ("input.md", "input.markdown"):
                    candidate = (
                        run_directory / "artifacts" / name
                    ).resolve()
                    if (
                        candidate.is_relative_to(campaign)
                        and candidate.is_file()
                    ):
                        return candidate
                input_directory = run_directory / "artifacts" / "input"
                if input_directory.is_dir():
                    for candidate in sorted(input_directory.iterdir()):
                        resolved = candidate.resolve()
                        if (
                            resolved.is_relative_to(campaign)
                            and resolved.is_file()
                            and resolved.suffix.lower()
                            in {".md", ".markdown"}
                        ):
                            return resolved
        task = manifest.get("task")
        if isinstance(task, Mapping):
            source = task.get("source_path")
            if isinstance(source, str):
                candidate = Path(source).resolve()
                if candidate.is_file():
                    return candidate
        return None


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign",
        required=True,
        help="Replay campaign directory containing replay.json",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Destination artifact-comparison.json",
    )
    parser.add_argument(
        "--source",
        help="Optional Markdown source; defaults to the campaign snapshot",
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="Capture fixed desktop and mobile screenshots with Chrome",
    )
    parser.add_argument(
        "--chrome",
        help="Chrome executable path or command name",
    )
    parser.add_argument(
        "--screenshot-timeout",
        type=float,
        default=30.0,
        help="Seconds allowed for each viewport capture",
    )
    options = parser.parse_args(arguments)
    output = Path(options.output).resolve()
    comparator = HTMLArtifactComparator(
        chrome_command=options.chrome,
        screenshot_timeout=options.screenshot_timeout,
    )
    report = comparator.compare_campaign(
        options.campaign,
        source_path=options.source,
        capture_screenshots=options.screenshots,
        screenshot_directory=(
            output.parent / "screenshots"
            if options.screenshots
            else None
        ),
    )
    _atomic_write_json(output, report)
    print(output)
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(_run_cli())
    except (
        ArtifactComparisonError,
        FileNotFoundError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
