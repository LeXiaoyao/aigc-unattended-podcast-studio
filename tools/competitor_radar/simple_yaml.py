from __future__ import annotations

from typing import Any


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "[]":
        return []
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].replace('\\"', '"').replace("\\'", "'")
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _clean_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))
    return lines


def parse_simple_yaml(text: str) -> Any:
    lines = _clean_lines(text)

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index
        if lines[index][0] < indent:
            return {}, index
        is_list = lines[index][1].startswith("- ")
        if is_list:
            result: list[Any] = []
            while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
                rest = lines[index][1][2:].strip()
                index += 1
                if not rest:
                    child, index = parse_block(index, indent + 2)
                    result.append(child)
                elif ":" in rest:
                    key, value = rest.split(":", 1)
                    item: dict[str, Any] = {key.strip(): parse_scalar(value.strip()) if value.strip() else {}}
                    while index < len(lines) and lines[index][0] > indent:
                        child_indent, child_text = lines[index]
                        if child_text.startswith("- "):
                            break
                        if ":" not in child_text:
                            index += 1
                            continue
                        child_key, child_value = child_text.split(":", 1)
                        child_key = child_key.strip()
                        child_value = child_value.strip()
                        index += 1
                        if child_value:
                            item[child_key] = parse_scalar(child_value)
                        else:
                            nested, index = parse_block(index, child_indent + 2)
                            item[child_key] = nested
                    result.append(item)
                else:
                    result.append(parse_scalar(rest))
            return result, index

        result: dict[str, Any] = {}
        while index < len(lines) and lines[index][0] == indent and not lines[index][1].startswith("- "):
            text_line = lines[index][1]
            if ":" not in text_line:
                index += 1
                continue
            key, value = text_line.split(":", 1)
            key = key.strip()
            value = value.strip()
            index += 1
            if value:
                result[key] = parse_scalar(value)
            else:
                child, index = parse_block(index, indent + 2)
                result[key] = child
        return result, index

    parsed, _ = parse_block(0, 0)
    return parsed

