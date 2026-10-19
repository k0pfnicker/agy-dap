"""Optional SymbolResolver interface for future agy-lsp integration.

In the MVP, file and line numbers work standalone without requiring a running
LSP session. Agents can query definitions via agy-lsp first and pass the
resolved file and line to agy-dap. This protocol defines the clean boundary
for optional programmatic symbol resolution without regex-based fake resolvers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ResolvedSymbolLocation:
    """Resolved file and 1-indexed line number from a semantic symbol provider."""

    symbol: str
    file: str
    line: int
    character: Optional[int] = None
    provider: str = "lsp"


@runtime_checkable
class SymbolResolver(Protocol):
    """Protocol for resolving a symbol identifier to a source file and line."""

    async def resolve_symbol(
        self,
        symbol: str,
        context_file: Optional[str] = None,
    ) -> Optional[ResolvedSymbolLocation]:
        """Resolve a symbol name to a source location, or return None if unavailable."""
        ...
