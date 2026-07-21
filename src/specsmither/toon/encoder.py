"""TOON (Token-Oriented Object Notation) encoder for the TOON format v2.1.0
(the MCP agent wire format).

TOON combines YAML-style indentation for nested objects with a CSV-style tabular
layout for uniform arrays of objects, producing a compact, lossless encoding of
the JSON data model. The encode path is **byte-for-byte deterministic** and
conforms to the TOON format spec; output is pinned by the committed golden
fixtures under ``fixtures/golden/toon/`` (see ``tests/test_toon_golden.py``).

Only ``encode`` is implemented — the MCP response path is encode-only (the JSON path
is plain ``json.dumps``). A callback-style ``replacer`` option is intentionally
omitted; ``indent``, ``delimiter``, ``key_folding`` and ``flatten_depth`` follow
the TOON option semantics.

Byte-level subtleties the format requires:

* **Number formatting** follows the ECMAScript ``Number::toString`` algorithm
  (``String(n)``), not Python's ``str(float)`` — e.g. ``0.000001`` (not
  ``1e-06``), ``1e+21`` (not ``1e21``), ``1e-7`` (not ``1e-07``). Integers use
  ``str(int)`` which already matches JS for the safe-integer range.
* **String trimming** uses the exact ECMAScript ``String.prototype.trim``
  whitespace set when deciding whether a string has leading/trailing whitespace
  (which forces quoting), not Python's ``str.strip`` set.
* **Object key order** is preserved as-iterated (insertion order). Inputs that
  flow through JSON serialization already carry JS's canonical key order, so the
  golden fixtures stay in parity; this encoder does not re-sort integer-like keys
  the way a live JS object would.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["DEFAULT_DELIMITER", "DELIMITERS", "encode"]

# --- constants ---------------------------------------------------------------

_LIST_ITEM_MARKER = "-"
_LIST_ITEM_PREFIX = "- "
_DOT = "."
_NULL_LITERAL = "null"
_TRUE_LITERAL = "true"
_FALSE_LITERAL = "false"

DELIMITERS: dict[str, str] = {"comma": ",", "tab": "\t", "pipe": "|"}
DEFAULT_DELIMITER = ","

# The exact whitespace set stripped by ECMAScript ``String.prototype.trim``
# (WhiteSpace + LineTerminator). Python's ``str.strip`` differs (it strips e.g.
# U+001C-U+001F and U+0085 but not U+FEFF), so we strip with this explicit set.
_JS_TRIM_WS = (
    "\t\n\x0b\x0c\r \xa0 "
    "           "
    "    　﻿"
)

# Unquoted key: starts with ASCII letter/underscore, then word chars or dots
# (``/^[A-Z_][\w.]*$/i`` with JS's ASCII ``\w``).
_UNQUOTED_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# Identifier segment for safe folding (``/^[A-Z_]\w*$/i``).
_IDENTIFIER_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Number-like strings that must be quoted (``isNumericLike``).
_NUMERIC_LIKE_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_LEADING_ZERO_RE = re.compile(r"^0[0-9]+$")
_STRUCTURAL_BRACKETS_RE = re.compile(r"[\[\]{}]")
_CONTROL_CHARS_RE = re.compile(r"[\n\r\t]")


@dataclass(frozen=True)
class _Options:
    indent: int
    delimiter: str
    key_folding: str
    flatten_depth: float


# --- string / literal utilities ----------------------------------------------


def _escape_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _is_boolean_or_null_literal(token: str) -> bool:
    return token in (_TRUE_LITERAL, _FALSE_LITERAL, _NULL_LITERAL)


def _is_numeric_like(value: str) -> bool:
    return bool(_NUMERIC_LIKE_RE.match(value)) or bool(_LEADING_ZERO_RE.match(value))


def _is_safe_unquoted(value: str, delimiter: str) -> bool:
    if not value:
        return False
    if value != value.strip(_JS_TRIM_WS):
        return False
    if _is_boolean_or_null_literal(value) or _is_numeric_like(value):
        return False
    if ":" in value:
        return False
    if '"' in value or "\\" in value:
        return False
    if _STRUCTURAL_BRACKETS_RE.search(value):
        return False
    if _CONTROL_CHARS_RE.search(value):
        return False
    if delimiter in value:
        return False
    return not value.startswith(_LIST_ITEM_MARKER)


def _is_valid_unquoted_key(key: str) -> bool:
    return bool(_UNQUOTED_KEY_RE.match(key))


# --- number formatting: ECMAScript Number::toString --------------------------


def _shortest_decimal(value: float) -> tuple[str, int]:
    """Return ``(digits, n)`` for a finite, positive float, where ``digits`` is
    the shortest round-tripping significant-digit string and ``n`` is the
    position of the decimal point such that the value equals
    ``int(digits) * 10**(n - len(digits))`` (ECMAScript's ``s`` and ``n``).

    Python's ``repr`` already yields the shortest round-tripping decimal (same
    digits as V8); we only re-derive the point position for ECMAScript layout.
    """
    text = repr(value)
    if "e" in text or "E" in text:
        mantissa, _, exp_str = text.lower().partition("e")
        exponent = int(exp_str)
    else:
        mantissa = text
        exponent = 0
    if "." in mantissa:
        int_part, frac_part = mantissa.split(".")
    else:
        int_part, frac_part = mantissa, ""
    digits_all = int_part + frac_part
    point_pos = len(int_part) + exponent
    stripped = digits_all.lstrip("0")
    point_pos -= len(digits_all) - len(stripped)
    digits = stripped.rstrip("0")
    if not digits:
        return "0", 1
    return digits, point_pos


def _js_float_to_string(value: float) -> str:
    if value == 0.0:
        return "0"
    sign = ""
    if value < 0:
        sign = "-"
        value = -value
    digits, n = _shortest_decimal(value)
    k = len(digits)
    if k <= n <= 21:
        result = digits + "0" * (n - k)
    elif 0 < n <= 21:
        result = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        result = "0." + "0" * (-n) + digits
    else:
        mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
        e = n - 1
        exp = "e+" + str(e) if e >= 0 else "e-" + str(-e)
        result = mantissa + exp
    return sign + result


def _number_to_string(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        return _NULL_LITERAL
    return _js_float_to_string(value)


# --- primitive / key encoding ------------------------------------------------


def _encode_primitive(value: Any, delimiter: str) -> str:
    if value is None:
        return _NULL_LITERAL
    if isinstance(value, bool):
        return _TRUE_LITERAL if value else _FALSE_LITERAL
    if isinstance(value, (int, float)):
        return _number_to_string(value)
    return _encode_string_literal(value, delimiter)


def _encode_string_literal(value: str, delimiter: str) -> str:
    if _is_safe_unquoted(value, delimiter):
        return value
    return '"' + _escape_string(value) + '"'


def _encode_key(key: str) -> str:
    if _is_valid_unquoted_key(key):
        return key
    return '"' + _escape_string(key) + '"'


def _encode_and_join_primitives(values: list[Any], delimiter: str) -> str:
    return delimiter.join(_encode_primitive(v, delimiter) for v in values)


def _format_header(
    length: int,
    *,
    key: str | None = None,
    fields: list[str] | None = None,
    delimiter: str = DEFAULT_DELIMITER,
) -> str:
    header = ""
    if key:
        header += _encode_key(key)
    header += "[" + str(length) + (delimiter if delimiter != DEFAULT_DELIMITER else "") + "]"
    if fields is not None:
        header += "{" + delimiter.join(_encode_key(f) for f in fields) + "}"
    header += ":"
    return header


# --- value-shape predicates --------------------------------------------------


def _is_json_primitive(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_json_array(value: Any) -> bool:
    return isinstance(value, list)


def _is_json_object(value: Any) -> bool:
    return isinstance(value, dict)


def _is_empty_object(value: dict[str, Any]) -> bool:
    return len(value) == 0


def _is_array_of_primitives(value: list[Any]) -> bool:
    return len(value) == 0 or all(_is_json_primitive(item) for item in value)


def _is_array_of_arrays(value: list[Any]) -> bool:
    return len(value) == 0 or all(_is_json_array(item) for item in value)


def _is_array_of_objects(value: list[Any]) -> bool:
    return len(value) == 0 or all(_is_json_object(item) for item in value)


# --- normalization (mirror normalizeValue for the JSON data model) -----------


def _normalize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if value == 0.0:
            return 0
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalize_value(v) for k, v in value.items()}
    return None


# --- key folding -------------------------------------------------------------


def _collect_single_key_chain(
    start_key: str, start_value: Any, max_depth: float
) -> tuple[list[str], Any, Any]:
    segments = [start_key]
    current = start_value
    while len(segments) < max_depth:
        if not _is_json_object(current):
            break
        keys = list(current.keys())
        if len(keys) != 1:
            break
        next_key = keys[0]
        current = current[next_key]
        segments.append(next_key)
    if not _is_json_object(current) or _is_empty_object(current):
        return segments, None, current
    return segments, current, current


def _try_fold_key_chain(
    key: str,
    value: Any,
    siblings: list[str],
    options: _Options,
    root_literal_keys: set[str] | None,
    path_prefix: str | None,
    flatten_depth: float | None,
) -> tuple[str, Any, Any, int] | None:
    if options.key_folding != "safe":
        return None
    if not _is_json_object(value):
        return None
    max_depth = flatten_depth if flatten_depth is not None else options.flatten_depth
    segments, tail, leaf_value = _collect_single_key_chain(key, value, max_depth)
    if len(segments) < 2:
        return None
    if not all(_IDENTIFIER_SEGMENT_RE.match(seg) for seg in segments):
        return None
    folded_key = _DOT.join(segments)
    absolute_path = f"{path_prefix}{_DOT}{folded_key}" if path_prefix else folded_key
    if folded_key in siblings:
        return None
    if root_literal_keys is not None and absolute_path in root_literal_keys:
        return None
    return folded_key, tail, leaf_value, len(segments)


# --- core encoders -----------------------------------------------------------


def _indented_line(depth: int, content: str, indent_size: int) -> str:
    return " " * (indent_size * depth) + content


def _indented_list_item(depth: int, content: str, indent_size: int) -> str:
    return _indented_line(depth, _LIST_ITEM_PREFIX + content, indent_size)


def _encode_json_value(value: Any, options: _Options, depth: int) -> Iterator[str]:
    if _is_json_primitive(value):
        encoded = _encode_primitive(value, options.delimiter)
        if encoded != "":
            yield encoded
        return
    if _is_json_array(value):
        yield from _encode_array_lines(None, value, depth, options)
    elif _is_json_object(value):
        yield from _encode_object_lines(value, depth, options)


def _encode_object_lines(
    value: dict[str, Any],
    depth: int,
    options: _Options,
    root_literal_keys: set[str] | None = None,
    path_prefix: str | None = None,
    remaining_depth: float | None = None,
) -> Iterator[str]:
    keys = list(value.keys())
    if depth == 0 and root_literal_keys is None:
        root_literal_keys = {k for k in keys if _DOT in k}
    effective = remaining_depth if remaining_depth is not None else options.flatten_depth
    for key, val in value.items():
        yield from _encode_key_value_pair_lines(
            key, val, depth, options, keys, root_literal_keys, path_prefix, effective
        )


def _encode_key_value_pair_lines(
    key: str,
    value: Any,
    depth: int,
    options: _Options,
    siblings: list[str] | None,
    root_literal_keys: set[str] | None,
    path_prefix: str | None,
    flatten_depth: float | None,
) -> Iterator[str]:
    current_path = f"{path_prefix}{_DOT}{key}" if path_prefix else key
    effective = flatten_depth if flatten_depth is not None else options.flatten_depth
    if options.key_folding == "safe" and siblings is not None:
        fold = _try_fold_key_chain(
            key, value, siblings, options, root_literal_keys, path_prefix, effective
        )
        if fold is not None:
            folded_key, remainder, leaf_value, segment_count = fold
            encoded_folded_key = _encode_key(folded_key)
            if remainder is None:
                if _is_json_primitive(leaf_value):
                    yield _indented_line(
                        depth,
                        f"{encoded_folded_key}: {_encode_primitive(leaf_value, options.delimiter)}",
                        options.indent,
                    )
                    return
                if _is_json_array(leaf_value):
                    yield from _encode_array_lines(folded_key, leaf_value, depth, options)
                    return
                if _is_json_object(leaf_value) and _is_empty_object(leaf_value):
                    yield _indented_line(depth, f"{encoded_folded_key}:", options.indent)
                    return
            if _is_json_object(remainder):
                yield _indented_line(depth, f"{encoded_folded_key}:", options.indent)
                next_remaining = effective - segment_count
                folded_path = (
                    f"{path_prefix}{_DOT}{folded_key}" if path_prefix else folded_key
                )
                yield from _encode_object_lines(
                    remainder, depth + 1, options, root_literal_keys, folded_path, next_remaining
                )
                return
    encoded_key = _encode_key(key)
    if _is_json_primitive(value):
        yield _indented_line(
            depth, f"{encoded_key}: {_encode_primitive(value, options.delimiter)}", options.indent
        )
    elif _is_json_array(value):
        yield from _encode_array_lines(key, value, depth, options)
    elif _is_json_object(value):
        yield _indented_line(depth, f"{encoded_key}:", options.indent)
        if not _is_empty_object(value):
            yield from _encode_object_lines(
                value, depth + 1, options, root_literal_keys, current_path, effective
            )


def _encode_array_lines(
    key: str | None, value: list[Any], depth: int, options: _Options
) -> Iterator[str]:
    if len(value) == 0:
        yield _indented_line(
            depth, _format_header(0, key=key, delimiter=options.delimiter), options.indent
        )
        return
    if _is_array_of_primitives(value):
        yield _indented_line(
            depth, _encode_inline_array_line(value, options.delimiter, key), options.indent
        )
        return
    if _is_array_of_arrays(value) and all(_is_array_of_primitives(arr) for arr in value):
        yield from _encode_array_of_arrays_as_list_items_lines(key, value, depth, options)
        return
    if _is_array_of_objects(value):
        header = _extract_tabular_header(value)
        if header is not None:
            yield from _encode_array_of_objects_as_tabular_lines(
                key, value, header, depth, options
            )
        else:
            yield from _encode_mixed_array_as_list_items_lines(key, value, depth, options)
        return
    yield from _encode_mixed_array_as_list_items_lines(key, value, depth, options)


def _encode_array_of_arrays_as_list_items_lines(
    prefix: str | None, values: list[Any], depth: int, options: _Options
) -> Iterator[str]:
    yield _indented_line(
        depth, _format_header(len(values), key=prefix, delimiter=options.delimiter), options.indent
    )
    for arr in values:
        if _is_array_of_primitives(arr):
            array_line = _encode_inline_array_line(arr, options.delimiter, None)
            yield _indented_list_item(depth + 1, array_line, options.indent)


def _encode_inline_array_line(values: list[Any], delimiter: str, prefix: str | None) -> str:
    header = _format_header(len(values), key=prefix, delimiter=delimiter)
    joined = _encode_and_join_primitives(values, delimiter)
    if len(values) == 0:
        return header
    return f"{header} {joined}"


def _encode_array_of_objects_as_tabular_lines(
    prefix: str | None,
    rows: list[Any],
    header: list[str],
    depth: int,
    options: _Options,
) -> Iterator[str]:
    yield _indented_line(
        depth,
        _format_header(len(rows), key=prefix, fields=header, delimiter=options.delimiter),
        options.indent,
    )
    yield from _write_tabular_rows_lines(rows, header, depth + 1, options)


def _extract_tabular_header(rows: list[Any]) -> list[str] | None:
    if len(rows) == 0:
        return None
    first_keys = list(rows[0].keys())
    if len(first_keys) == 0:
        return None
    if _is_tabular_array(rows, first_keys):
        return first_keys
    return None


def _is_tabular_array(rows: list[Any], header: list[str]) -> bool:
    for row in rows:
        if len(row) != len(header):
            return False
        for key in header:
            if key not in row:
                return False
            if not _is_json_primitive(row[key]):
                return False
    return True


def _write_tabular_rows_lines(
    rows: list[Any], header: list[str], depth: int, options: _Options
) -> Iterator[str]:
    for row in rows:
        yield _indented_line(
            depth,
            _encode_and_join_primitives([row[key] for key in header], options.delimiter),
            options.indent,
        )


def _encode_mixed_array_as_list_items_lines(
    prefix: str | None, items: list[Any], depth: int, options: _Options
) -> Iterator[str]:
    yield _indented_line(
        depth, _format_header(len(items), key=prefix, delimiter=options.delimiter), options.indent
    )
    for item in items:
        yield from _encode_list_item_value_lines(item, depth + 1, options)


def _encode_object_as_list_item_lines(
    obj: dict[str, Any], depth: int, options: _Options
) -> Iterator[str]:
    if _is_empty_object(obj):
        yield _indented_line(depth, _LIST_ITEM_MARKER, options.indent)
        return
    entries = list(obj.items())
    first_key, first_value = entries[0]
    rest_entries = entries[1:]
    if _is_json_array(first_value) and _is_array_of_objects(first_value):
        header = _extract_tabular_header(first_value)
        if header is not None:
            yield _indented_list_item(
                depth,
                _format_header(
                    len(first_value), key=first_key, fields=header, delimiter=options.delimiter
                ),
                options.indent,
            )
            yield from _write_tabular_rows_lines(first_value, header, depth + 2, options)
            if len(rest_entries) > 0:
                yield from _encode_object_lines(dict(rest_entries), depth + 1, options)
            return
    encoded_key = _encode_key(first_key)
    if _is_json_primitive(first_value):
        yield _indented_list_item(
            depth,
            f"{encoded_key}: {_encode_primitive(first_value, options.delimiter)}",
            options.indent,
        )
    elif _is_json_array(first_value):
        if len(first_value) == 0:
            yield _indented_list_item(
                depth,
                f"{encoded_key}{_format_header(0, delimiter=options.delimiter)}",
                options.indent,
            )
        elif _is_array_of_primitives(first_value):
            yield _indented_list_item(
                depth,
                f"{encoded_key}{_encode_inline_array_line(first_value, options.delimiter, None)}",
                options.indent,
            )
        else:
            yield _indented_list_item(
                depth,
                f"{encoded_key}{_format_header(len(first_value), delimiter=options.delimiter)}",
                options.indent,
            )
            for item in first_value:
                yield from _encode_list_item_value_lines(item, depth + 2, options)
    elif _is_json_object(first_value):
        yield _indented_list_item(depth, f"{encoded_key}:", options.indent)
        if not _is_empty_object(first_value):
            yield from _encode_object_lines(first_value, depth + 2, options)
    if len(rest_entries) > 0:
        yield from _encode_object_lines(dict(rest_entries), depth + 1, options)


def _encode_list_item_value_lines(value: Any, depth: int, options: _Options) -> Iterator[str]:
    if _is_json_primitive(value):
        yield _indented_list_item(
            depth, _encode_primitive(value, options.delimiter), options.indent
        )
    elif _is_json_array(value):
        if _is_array_of_primitives(value):
            yield _indented_list_item(
                depth, _encode_inline_array_line(value, options.delimiter, None), options.indent
            )
        else:
            yield _indented_list_item(
                depth, _format_header(len(value), delimiter=options.delimiter), options.indent
            )
            for item in value:
                yield from _encode_list_item_value_lines(item, depth + 1, options)
    elif _is_json_object(value):
        yield from _encode_object_as_list_item_lines(value, depth, options)


# --- public API --------------------------------------------------------------


def encode(
    value: Any,
    *,
    indent: int = 2,
    delimiter: str = DEFAULT_DELIMITER,
    key_folding: Literal["off", "safe"] = "off",
    flatten_depth: float = math.inf,
) -> str:
    """Encode a JSON-compatible value into a TOON-format string.

    The value is normalized to the JSON data model, then emitted as TOON lines
    joined by ``"\\n"``.

    Args:
        value: Any JSON-compatible value (dict, list, str, int, float, bool,
            None; tuples are treated as arrays).
        indent: Spaces per indentation level (default 2).
        delimiter: Row/inline-array delimiter — ``","``, ``"\\t"`` or ``"|"``
            (default ``","``).
        key_folding: ``"off"`` or ``"safe"`` — collapse single-key wrapper
            chains into dotted paths (default ``"off"``).
        flatten_depth: Maximum segments to fold when ``key_folding="safe"``
            (default unbounded).

    Returns:
        The TOON-encoded string (no trailing newline).
    """
    options = _Options(
        indent=indent,
        delimiter=delimiter,
        key_folding=key_folding,
        flatten_depth=flatten_depth,
    )
    normalized = _normalize_value(value)
    return "\n".join(_encode_json_value(normalized, options, 0))
