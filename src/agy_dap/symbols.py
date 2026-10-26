"""SymbolResolver implementations connecting agy-dap to agy-lsp and Python AST.

Allows agents to specify breakpoints by function, method, or class symbol name
(e.g. `{"file": "src/checkout.py", "symbol": "calculate_total"}` or with `line_offset`)
in addition to explicit 1-indexed line numbers.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from agy_dap.utils.logging import get_logger
from agy_dap.utils.paths import ensure_workspace_path, normalize_path

logger = get_logger("agy-dap.symbols")


@dataclass(frozen=True)
class ResolvedSymbolLocation:
    """Resolved file, executable breakpoint line, and declaration line from a symbol provider."""

    symbol: str
    file: str
    line: int
    declaration_line: Optional[int] = None
    character: Optional[int] = None
    end_line: Optional[int] = None
    provider: str = "lsp"


@runtime_checkable
class SymbolResolver(Protocol):
    """Protocol for resolving a symbol identifier to a source file and executable line."""

    async def resolve_symbol(
        self,
        symbol: str,
        context_file: Optional[str] = None,
    ) -> Optional[ResolvedSymbolLocation]:
        """Resolve a symbol name to a source location, or return None if unavailable."""
        ...


def _first_executable_body_line(node: ast.AST) -> int:
    """Return the 1-indexed line of the first executable statement inside a function/class body."""
    lineno = getattr(node, "lineno", 1)
    body = getattr(node, "body", None)
    if not isinstance(body, list) or not body:
        return lineno

    # Skip leading docstring if followed by real statements
    if (
        len(body) > 1
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return getattr(body[1], "lineno", lineno)
    return getattr(body[0], "lineno", lineno)


class PythonAstSymbolResolver:
    """Deterministic Python AST symbol resolver requiring no external language server process."""

    def __init__(self, workspace_root: str, allow_outside: bool = False) -> None:
        self.workspace_root = normalize_path(workspace_root)
        self.allow_outside = allow_outside

    async def resolve_symbol(
        self,
        symbol: str,
        context_file: Optional[str] = None,
    ) -> Optional[ResolvedSymbolLocation]:
        cleaned = symbol.strip()
        if not cleaned or not context_file:
            return None

        resolved_path = ensure_workspace_path(
            context_file,
            self.workspace_root,
            allow_outside=self.allow_outside,
            must_exist=True,
        )
        p = Path(resolved_path)
        if p.suffix.lower() not in (".py", ".pyi"):
            return None

        try:
            source = p.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=resolved_path)
        except Exception as e:
            logger.debug(f"Python AST parse failed for {resolved_path}: {e}")
            return None

        parts = [part.strip() for part in cleaned.replace("::", ".").split(".") if part.strip()]
        if not parts:
            return None

        matched = self._find_ast_node(tree.body, parts)
        if matched is None:
            return None

        decl_line = getattr(matched, "lineno", 1)
        exec_line = _first_executable_body_line(matched)
        end_line = getattr(matched, "end_lineno", exec_line)
        col = getattr(matched, "col_offset", 0) + 1

        return ResolvedSymbolLocation(
            symbol=cleaned,
            file=resolved_path,
            line=exec_line,
            declaration_line=decl_line,
            character=col,
            end_line=end_line,
            provider="python-ast",
        )

    def _find_ast_node(self, statements: list[ast.stmt], parts: list[str]) -> Optional[ast.AST]:
        target = parts[0]
        rest = parts[1:]

        for stmt in statements:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if stmt.name == target:
                    if not rest:
                        return stmt
                    return self._find_ast_node(stmt.body, rest)
            # If searching unqualified method name ('add' instead of 'Calculator.add')
            if len(parts) == 1 and isinstance(stmt, ast.ClassDef):
                nested = self._find_ast_node(stmt.body, parts)
                if nested is not None:
                    return nested
        return None


def _ensure_agy_lsp_importable() -> bool:
    """Ensure companion plugin `agy_lsp` is importable from site-packages, plugin dir, or sibling repo."""
    if importlib.util.find_spec("agy_lsp") is not None:
        return True

    candidates = [
        Path.home() / ".gemini" / "config" / "plugins" / "agy-lsp" / "src",
        Path(__file__).resolve().parents[3] / "agy-lsp" / "src",
    ]
    for cand in candidates:
        if (cand / "agy_lsp" / "__init__.py").is_file():
            cand_str = str(cand)
            if cand_str not in sys.path:
                sys.path.insert(0, cand_str)
            return importlib.util.find_spec("agy_lsp") is not None
    return False


class AgyLspSymbolResolver:
    """SymbolResolver backed by companion plugin `agy-lsp` when available in the environment."""

    def __init__(self, workspace_root: str, allow_outside: bool = False) -> None:
        self.workspace_root = normalize_path(workspace_root)
        self.allow_outside = allow_outside

    async def resolve_symbol(
        self,
        symbol: str,
        context_file: Optional[str] = None,
    ) -> Optional[ResolvedSymbolLocation]:
        if not _ensure_agy_lsp_importable():
            return None
        try:
            from agy_lsp.config import PluginConfig as LspPluginConfig
            from agy_lsp.lsp.manager import LspManager
            from agy_lsp.tools.symbols import resolve_symbol_target
        except ImportError:
            return None

        resolved_ctx: Optional[str] = None
        if context_file:
            resolved_ctx = ensure_workspace_path(
                context_file,
                self.workspace_root,
                allow_outside=self.allow_outside,
                must_exist=True,
            )

        lsp_cfg = LspPluginConfig(
            workspaceRoot=self.workspace_root,
            allowOutsideWorkspace=self.allow_outside,
        )
        manager = LspManager(lsp_cfg)
        try:
            res = await resolve_symbol_target(
                manager=manager,
                config=lsp_cfg,
                symbol=symbol,
                context_file=resolved_ctx,
            )
            if not res:
                return None
            decl_line = int(res.get("line", 1))
            body_line = int(res.get("body_line", decl_line))
            return ResolvedSymbolLocation(
                symbol=symbol.strip(),
                file=str(res.get("file") or resolved_ctx or ""),
                line=body_line,
                declaration_line=decl_line,
                character=int(res.get("column", 1)),
                end_line=int(res.get("end_line", body_line)),
                provider="agy-lsp",
            )
        except Exception as e:
            logger.debug(f"agy-lsp symbol resolution skipped for '{symbol}': {e}")
            return None
        finally:
            try:
                await manager.shutdown_all()
            except Exception:
                pass


class CSharpSymbolResolver:
    """Deterministic structural symbol resolver for C# (.cs) files when a C# LSP is not running."""

    _METHOD_OR_CLASS_RE = re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|async|sealed|partial|abstract)\s+)*"
        r"(?:class|struct|record|interface|[A-Za-z_][A-Za-z0-9_<>,.\[\]?]*)\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]+>)?\s*(?:\(|{|\:|$)"
    )

    def __init__(self, workspace_root: str, allow_outside: bool = False) -> None:
        self.workspace_root = normalize_path(workspace_root)
        self.allow_outside = allow_outside

    async def resolve_symbol(
        self,
        symbol: str,
        context_file: Optional[str] = None,
    ) -> Optional[ResolvedSymbolLocation]:
        cleaned = symbol.strip()
        if not cleaned or not context_file:
            return None

        resolved_path = ensure_workspace_path(
            context_file,
            self.workspace_root,
            allow_outside=self.allow_outside,
            must_exist=True,
        )
        p = Path(resolved_path)
        if p.suffix.lower() != ".cs":
            return None

        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return None

        parts = [part.strip() for part in cleaned.replace("::", ".").split(".") if part.strip()]
        if not parts:
            return None
        target_name = parts[-1]

        for idx, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("["):
                continue
            m = self._METHOD_OR_CLASS_RE.match(raw_line)
            if m and m.group(1) == target_name:
                decl_line = idx + 1
                exec_line = decl_line
                for look_ahead in range(idx + 1, min(len(lines), idx + 15)):
                    cand = lines[look_ahead].strip()
                    if (
                        not cand
                        or cand == "{"
                        or cand.startswith("//")
                        or cand.startswith("/*")
                        or cand.startswith("*")
                    ):
                        continue
                    exec_line = look_ahead + 1
                    break
                return ResolvedSymbolLocation(
                    symbol=cleaned,
                    file=resolved_path,
                    line=exec_line,
                    declaration_line=decl_line,
                    character=1,
                    end_line=exec_line,
                    provider="csharp-ast",
                )
        return None


class CompositeSymbolResolver:
    """Combines fast Python/C# AST resolution with `agy-lsp` multi-language resolution."""

    def __init__(self, workspace_root: str, allow_outside: bool = False) -> None:
        self.workspace_root = normalize_path(workspace_root)
        self.allow_outside = allow_outside
        self.ast_resolver = PythonAstSymbolResolver(workspace_root=self.workspace_root, allow_outside=allow_outside)
        self.csharp_resolver = CSharpSymbolResolver(workspace_root=self.workspace_root, allow_outside=allow_outside)
        self.lsp_resolver = AgyLspSymbolResolver(workspace_root=self.workspace_root, allow_outside=allow_outside)

    async def resolve_symbol(
        self,
        symbol: str,
        context_file: Optional[str] = None,
    ) -> Optional[ResolvedSymbolLocation]:
        # 1. For Python files with context_file, PythonAstSymbolResolver is instant (<1ms) and zero-process
        if context_file and str(context_file).lower().endswith((".py", ".pyi")):
            ast_loc = await self.ast_resolver.resolve_symbol(symbol, context_file=context_file)
            if ast_loc is not None:
                return ast_loc

        # 2. Try companion agy-lsp resolver (supports C#, C/C++, TS, Rust, Go, and workspace-wide queries)
        lsp_loc = await self.lsp_resolver.resolve_symbol(symbol, context_file=context_file)
        if lsp_loc is not None:
            return lsp_loc

        # 3. For C# files, fallback to deterministic CSharpSymbolResolver
        if context_file and str(context_file).lower().endswith(".cs"):
            cs_loc = await self.csharp_resolver.resolve_symbol(symbol, context_file=context_file)
            if cs_loc is not None:
                return cs_loc

        # 4. Fallback to AST resolver if not tried yet
        return await self.ast_resolver.resolve_symbol(symbol, context_file=context_file)
