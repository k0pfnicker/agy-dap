"""DAP Adapter Capabilities evaluation and enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agy_dap.errors import DapPluginError, ErrorCode


@dataclass
class AdapterCapabilities:
    """Evaluates capabilities returned by the debug adapter during initialize."""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def supports_configuration_done(self) -> bool:
        return bool(self.raw.get("supportsConfigurationDoneRequest", False))

    @property
    def supports_conditional_breakpoints(self) -> bool:
        return bool(self.raw.get("supportsConditionalBreakpoints", False))

    @property
    def supports_hit_conditional_breakpoints(self) -> bool:
        return bool(self.raw.get("supportsHitConditionalBreakpoints", False))

    @property
    def supports_function_breakpoints(self) -> bool:
        return bool(self.raw.get("supportsFunctionBreakpoints", False))

    @property
    def supports_evaluate_for_hovers(self) -> bool:
        return bool(self.raw.get("supportsEvaluateForHovers", False))

    @property
    def supports_terminate_request(self) -> bool:
        return bool(self.raw.get("supportsTerminateRequest", False))

    @property
    def supports_value_formatting_options(self) -> bool:
        return bool(self.raw.get("supportsValueFormattingOptions", False))

    @property
    def exception_breakpoint_filters(self) -> set[str]:
        filters = self.raw.get("exceptionBreakpointFilters", [])
        if not isinstance(filters, list):
            return set()
        result: set[str] = set()
        for item in filters:
            if isinstance(item, dict) and "filter" in item:
                result.add(str(item["filter"]))
        return result

    def require(self, capability_key: str, feature_name: str) -> None:
        """Raise CAPABILITY_UNSUPPORTED if the adapter did not advertise capability_key."""
        if not self.raw.get(capability_key, False):
            raise DapPluginError(
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                message=f"The debug adapter does not support '{feature_name}' (missing capability '{capability_key}').",
                retryable=False,
                details={
                    "required_capability": capability_key,
                    "feature": feature_name,
                    "adapter_capabilities": sorted(k for k, v in self.raw.items() if v),
                },
            )
