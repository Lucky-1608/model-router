"""Strict JSON Schema validator for tool input arguments."""

from __future__ import annotations

from typing import Any

from gateway.core.exceptions import GatewayError
from fastapi import status


class ToolInputValidationError(GatewayError):
    """Raised when tool arguments fail JSON Schema validation."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_type = "tool_validation_error"
    error_code = "GW_TOOL_SCHEMA_INVALID"
    retryable = False


class ToolPermissionError(GatewayError):
    """Raised when a caller or request is not permitted to invoke a tool."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "tool_permission_error"
    error_code = "GW_TOOL_FORBIDDEN"
    retryable = False


class ToolExecutionError(GatewayError):
    """Raised when a gateway-managed or MCP tool fails or times out."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "tool_execution_error"
    error_code = "GW_TOOL_EXECUTION_FAILED"
    retryable = True


def validate_tool_arguments(
    tool_name: str,
    arguments: Any,
    schema: dict[str, Any],
) -> None:
    """Validates tool arguments against a JSON Schema subset deterministically.

    Raises ToolInputValidationError if any constraint is violated.
    """
    errors: list[str] = []
    _validate_node(arguments, schema, path="$", errors=errors)
    if errors:
        raise ToolInputValidationError(
            f"Tool {tool_name!r} arguments failed JSON Schema validation: {'; '.join(errors)}",
            param=tool_name,
            details={"tool_name": tool_name, "schema_errors": errors},
        )


def _validate_node(
    instance: Any,
    schema: dict[str, Any],
    *,
    path: str,
    errors: list[str],
) -> None:
    if not isinstance(schema, dict) or not schema:
        return

    expected_type = schema.get("type")
    if expected_type is not None:
        allowed_types = (
            expected_type if isinstance(expected_type, list) else [expected_type]
        )
        if not any(_matches_json_type(instance, t) for t in allowed_types):
            errors.append(
                f"{path}: expected type {expected_type!r}, got {type(instance).__name__}"
            )
            return

    if "enum" in schema:
        enum_vals = schema["enum"]
        if instance not in enum_vals:
            errors.append(f"{path}: value {instance!r} not in enum {enum_vals!r}")

    if isinstance(instance, str):
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if min_len is not None and len(instance) < int(min_len):
            errors.append(f"{path}: string length {len(instance)} < minLength {min_len}")
        if max_len is not None and len(instance) > int(max_len):
            errors.append(f"{path}: string length {len(instance)} > maxLength {max_len}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and instance < float(minimum):
            errors.append(f"{path}: value {instance} < minimum {minimum}")
        if maximum is not None and instance > float(maximum):
            errors.append(f"{path}: value {instance} > maximum {maximum}")

    if isinstance(instance, dict):
        required_fields = schema.get("required", [])
        for req_field in required_fields:
            if req_field not in instance:
                errors.append(f"{path}: missing required property {req_field!r}")

        properties = schema.get("properties", {})
        additional_props = schema.get("additionalProperties", True)
        for key, val in instance.items():
            prop_path = f"{path}.{key}"
            if key in properties and isinstance(properties[key], dict):
                _validate_node(val, properties[key], path=prop_path, errors=errors)
            elif additional_props is False:
                errors.append(f"{path}: unexpected additional property {key!r}")

    if isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, elem in enumerate(instance):
                _validate_node(
                    elem, item_schema, path=f"{path}[{idx}]", errors=errors
                )


def _matches_json_type(val: Any, json_type: str) -> bool:
    if json_type == "object":
        return isinstance(val, dict)
    if json_type == "string":
        return isinstance(val, str)
    if json_type == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if json_type == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if json_type == "boolean":
        return isinstance(val, bool)
    if json_type == "array":
        return isinstance(val, list)
    if json_type == "null":
        return val is None
    return True

