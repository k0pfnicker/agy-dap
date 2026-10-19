"""Variable extraction, primitive parsing, depth-bounded traversal, and redaction."""

from __future__ import annotations

import ast
from typing import Any, Optional

from agy_dap.config import LimitsConfig
from agy_dap.dap.client import DapClient
from agy_dap.utils.redaction import REDACTED_PLACEHOLDER, is_sensitive_key, redact_value_if_needed

# Special debugpy synthetic category names to skip when enumerating frame locals
_SYNTHETIC_VAR_NAMES = {
    "special variables",
    "function variables",
    "class variables",
    "__builtins__",
    "__doc__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__spec__",
    "__cached__",
}


def parse_dap_scalar(raw_value: Any, var_type: Optional[str]) -> tuple[Any, str]:
    """Convert DAP string representation of scalar Python values into native JSON scalars.

    Preserves important falsy values: False, 0, 0.0, "", and None (status='null').
    Returns (parsed_value, status).
    """
    if raw_value is None:
        return None, "null"

    if not isinstance(raw_value, str):
        return raw_value, "ok"

    vtype = (var_type or "").strip()

    if vtype == "NoneType" or raw_value == "None":
        return None, "null"

    if vtype == "bool" or raw_value in ("True", "False"):
        if raw_value == "True":
            return True, "ok"
        if raw_value == "False":
            return False, "ok"

    if vtype == "int":
        try:
            return int(raw_value), "ok"
        except ValueError:
            pass

    if vtype == "float":
        try:
            return float(raw_value), "ok"
        except ValueError:
            pass

    if vtype == "str":
        # debugpy formats strings as Python reprs, e.g. "'hello'" or "''"
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in ("'", '"'):
            try:
                unquoted = ast.literal_eval(raw_value)
                if isinstance(unquoted, str):
                    return unquoted, "ok"
            except Exception:
                pass
        return raw_value, "ok"

    return raw_value, "ok"


def truncate_string_value(val: Any, max_len: int) -> tuple[Any, bool]:
    """Truncate string values exceeding max_len while preserving empty strings."""
    if isinstance(val, str) and len(val) > max_len:
        return val[:max_len] + f"... [truncated, total_len={len(val)}]", True
    return val, False


async def fetch_scope_variables(
    client: DapClient,
    variables_reference: int,
    limits: LimitsConfig,
    requested_names: Optional[list[str]] = None,
) -> tuple[dict[str, dict[str, Any]], bool, list[str]]:
    """Fetch variables for a scope reference with bounded depth, item counts, and redaction.

    Returns (locals_dict, any_truncated, truncation_hints).
    """
    if variables_reference <= 0:
        return {}, False, []

    resp = await client.send_request("variables", {"variablesReference": variables_reference})
    raw_vars = resp.get("variables", [])
    if not isinstance(raw_vars, list):
        raw_vars = []

    # Filter out debugpy synthetic groupings and dunders unless explicitly requested
    requested_set = set(requested_names) if requested_names else None
    filtered_vars: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}

    for item in raw_vars:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if not name:
            continue
        by_name[name] = item
        if requested_set is not None:
            if name in requested_set:
                filtered_vars.append(item)
        else:
            if name in _SYNTHETIC_VAR_NAMES or (name.startswith("__") and name.endswith("__")):
                continue
            filtered_vars.append(item)

    any_truncated = False
    hints: list[str] = []
    result: dict[str, dict[str, Any]] = {}

    if requested_names is not None:
        # Preserve requested order and explicitly mark missing variables
        for req_name in requested_names:
            if req_name in by_name:
                entry_dict, entry_trunc = await _format_variable_item(
                    client=client,
                    item=by_name[req_name],
                    limits=limits,
                    current_depth=0,
                )
                result[req_name] = entry_dict
                if entry_trunc:
                    any_truncated = True
            else:
                result[req_name] = {
                    "value": None,
                    "type": None,
                    "status": "missing",
                    "hint": f"Variable '{req_name}' is not defined in this frame's local scope.",
                }
        return result, any_truncated, hints

    # Unfiltered locals: enforce maxVariablesPerScope
    total_count = len(filtered_vars)
    if total_count > limits.maxVariablesPerScope:
        any_truncated = True
        omitted = total_count - limits.maxVariablesPerScope
        hints.append(
            f"Omitted {omitted} local variable(s) (limit={limits.maxVariablesPerScope}). "
            "Pass explicit 'locals=[...]' names to inspect specific variables."
        )
        filtered_vars = filtered_vars[: limits.maxVariablesPerScope]

    for item in filtered_vars:
        name = str(item.get("name", ""))
        entry_dict, entry_trunc = await _format_variable_item(
            client=client,
            item=item,
            limits=limits,
            current_depth=0,
        )
        result[name] = entry_dict
        if entry_trunc:
            any_truncated = True

    if any_truncated and not hints:
        hints.append(
            "One or more variable values or nested containers were truncated; request specific locals if needed."
        )

    return result, any_truncated, hints


async def _format_variable_item(
    client: DapClient,
    item: dict[str, Any],
    limits: LimitsConfig,
    current_depth: int,
) -> tuple[dict[str, Any], bool]:
    """Format a single DAP Variable object, expanding structured children up to maxVariableDepth."""
    name = str(item.get("name", ""))
    raw_val = item.get("value")
    var_type = str(item.get("type")) if item.get("type") is not None else None
    var_ref = int(item.get("variablesReference", 0) or 0)

    # 1. Check key-based or value-based secret redaction first
    if is_sensitive_key(name):
        return {
            "value": REDACTED_PLACEHOLDER,
            "type": var_type,
            "status": "redacted",
        }, False

    parsed_val, status = parse_dap_scalar(raw_val, var_type)
    redacted_val, was_redacted = redact_value_if_needed(name, parsed_val)
    if was_redacted:
        return {
            "value": redacted_val,
            "type": var_type,
            "status": "redacted",
        }, False

    val_after_trunc, str_truncated = truncate_string_value(parsed_val, limits.maxStringLength)
    was_truncated = str_truncated

    entry: dict[str, Any] = {
        "value": val_after_trunc,
        "type": var_type,
        "status": status,
    }

    # 2. Check structured object / container children
    if var_ref > 0 and status != "null":
        if current_depth >= limits.maxVariableDepth:
            entry["status"] = "not_loaded"
            entry["truncated"] = True
            entry["hint"] = f"Nested structure not expanded beyond depth {limits.maxVariableDepth}."
            return entry, True

        try:
            child_resp = await client.send_request("variables", {"variablesReference": var_ref})
            raw_children = child_resp.get("variables", [])
            if not isinstance(raw_children, list):
                raw_children = []
        except Exception:
            entry["status"] = "not_loaded"
            entry["truncated"] = True
            return entry, True

        real_children = [
            c
            for c in raw_children
            if isinstance(c, dict)
            and str(c.get("name", "")) not in _SYNTHETIC_VAR_NAMES
            and not (str(c.get("name", "")).startswith("__") and str(c.get("name", "")).endswith("__"))
            and str(c.get("name", "")) != "len()"
        ]

        children_map: dict[str, Any] = {}
        if len(real_children) > limits.maxContainerItems:
            was_truncated = True
            entry["hint"] = f"Container truncated to {limits.maxContainerItems} of {len(real_children)} items."
            real_children = real_children[: limits.maxContainerItems]

        for child in real_children:
            cname = str(child.get("name", ""))
            child_entry, child_trunc = await _format_variable_item(
                client=client,
                item=child,
                limits=limits,
                current_depth=current_depth + 1,
            )
            children_map[cname] = child_entry
            if child_trunc:
                was_truncated = True

        if children_map:
            entry["children"] = children_map

    if was_truncated:
        entry["truncated"] = True

    return entry, was_truncated
