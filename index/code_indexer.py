"""Lightweight code indexer using AST for Python + regex fallback.

Optionally uses tree-sitter for accurate JS/TS/Go/Rust/Java parsing.
Falls back to regex if tree-sitter is not installed.
"""

import ast
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

# Optional tree-sitter import
_TREE_SITTER = None
_TREE_SITTER_LANGS = {}
try:
    # Try newer tree-sitter-languages package first
    import tree_sitter_languages  # type: ignore
    _TREE_SITTER = "languages"
except ImportError:
    try:
        # Fall back to bare tree-sitter package
        import tree_sitter  # type: ignore
        _TREE_SITTER = "bare"
    except ImportError:
        pass


@dataclass
class Symbol:
    kind: str  # function, class, method, variable
    name: str
    line: int
    col: int = 0


@dataclass
class FileIndex:
    path: str
    symbols: List[Symbol] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)
    language: str = ""


class CodeIndexer:
    """Index a codebase for fast symbol and content retrieval."""

    LANGUAGE_MAP = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
    }

    def __init__(self, root_dir: str = "."):
        self.root_dir = Path(root_dir).resolve()
        self.files: Dict[str, FileIndex] = {}

    def index_project(self, patterns: Optional[List[str]] = None):
        """Index all matching files under root_dir."""
        patterns = patterns or ["*.py", "*.js", "*.ts", "*.go", "*.rs", "*.java"]
        for pattern in patterns:
            for filepath in self.root_dir.rglob(pattern):
                if self._should_skip(filepath):
                    continue
                self._index_file(filepath)

    def _should_skip(self, filepath: Path) -> bool:
        """Skip common non-source directories."""
        skip_dirs = {
            "__pycache__", ".git", "node_modules", "venv", ".venv",
            "dist", "build", ".pytest_cache", ".mypy_cache",
        }
        return any(part in skip_dirs for part in filepath.parts)

    def _index_file(self, filepath: Path):
        """Index a single file."""
        ext = filepath.suffix
        language = self.LANGUAGE_MAP.get(ext, "")

        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return

        lines = content.splitlines()
        file_index = FileIndex(
            path=str(filepath.relative_to(self.root_dir)),
            lines=lines,
            language=language,
        )

        if language == "python":
            file_index.symbols = self._parse_python(content)
        elif _TREE_SITTER and language in ("javascript", "typescript", "tsx", "go", "rust", "java"):
            file_index.symbols = self._parse_tree_sitter(content, language)
        else:
            file_index.symbols = self._parse_generic(content, ext)

        self.files[file_index.path] = file_index

    def _parse_python(self, content: str) -> List[Symbol]:
        """Parse Python file with AST."""
        symbols = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return symbols

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                symbols.append(Symbol("class", node.name, node.lineno, node.col_offset))
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        symbols.append(Symbol("method", f"{node.name}.{item.name}", item.lineno, item.col_offset))
                    elif isinstance(item, ast.AsyncFunctionDef):
                        symbols.append(Symbol("method", f"{node.name}.{item.name}", item.lineno, item.col_offset))
                    elif isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                symbols.append(Symbol("variable", f"{node.name}.{target.id}", item.lineno, item.col_offset))
            elif isinstance(node, ast.FunctionDef):
                symbols.append(Symbol("function", node.name, node.lineno, node.col_offset))
            elif isinstance(node, ast.AsyncFunctionDef):
                symbols.append(Symbol("function", node.name, node.lineno, node.col_offset))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.append(Symbol("variable", target.id, node.lineno, node.col_offset))

        return symbols

    def _parse_generic(self, content: str, ext: str) -> List[Symbol]:
        """Regex-based parsing for non-Python languages."""
        import re
        symbols = []
        patterns = {
            ".js": [r"function\s+(\w+)", r"class\s+(\w+)", r"const\s+(\w+)\s*="],
            ".ts": [r"function\s+(\w+)", r"class\s+(\w+)", r"const\s+(\w+)\s*="],
            ".go": [r"func\s+(?:\([^)]+\)\s+)?(\w+)", r"type\s+(\w+)\s+struct"],
            ".rs": [r"fn\s+(\w+)", r"struct\s+(\w+)", r"impl\s+(\w+)"],
            ".java": [r"(?:public\s+|private\s+|protected\s+)?(?:class|interface)\s+(\w+)", r"(?:public\s+|private\s+|protected\s+)?[\w<>\[\]]+\s+(\w+)\s*\("],
        }

        regexes = patterns.get(ext, [])
        for i, line in enumerate(content.splitlines(), 1):
            for regex in regexes:
                for match in re.finditer(regex, line):
                    name = match.group(1)
                    kind = "function" if "func" in regex or "fn " in regex or "(" in line else "class"
                    if "struct" in regex:
                        kind = "class"
                    symbols.append(Symbol(kind, name, i, match.start()))

        return symbols

    def _parse_tree_sitter(self, content: str, language: str) -> List[Symbol]:
        """Parse source with tree-sitter for accurate symbol extraction."""
        symbols: List[Symbol] = []

        # Map our language names to tree-sitter language names
        lang_map = {
            "javascript": "javascript", "typescript": "typescript",
            "tsx": "tsx", "go": "go", "rust": "rust", "java": "java",
        }
        ts_lang = lang_map.get(language, language)

        try:
            if _TREE_SITTER == "languages":
                parser = tree_sitter_languages.Parser()
                parser.set_language(tree_sitter_languages.get_language(ts_lang))
            elif _TREE_SITTER == "bare":
                # Bare tree-sitter: language must be built manually
                return symbols  # Fallback to regex
            else:
                return symbols

            tree = parser.parse(content.encode("utf-8"))
            root = tree.root_node

            # Walk the tree for function/class/variable declarations
            _node_types = {
                "function": {"function_declaration", "method_definition",
                             "arrow_function", "function_expression"},
                "class": {"class_declaration", "class_definition"},
                "variable": {"variable_declaration", "variable_declarator",
                             "lexical_declaration", "const_declaration",
                             "let_declaration", "var_declaration"},
            }

            # Use cursor for efficient traversal
            cursor = root.walk()
            visited = set()
            while True:
                node = cursor.node
                if node.id not in visited:
                    visited.add(node.id)
                    node_type = node.type

                    # Determine symbol kind
                    if node_type in _node_types["function"]:
                        kind = "function"
                    elif node_type in _node_types["class"]:
                        kind = "class"
                    elif node_type in _node_types["variable"]:
                        kind = "variable"
                    else:
                        kind = ""

                    if kind:
                        # Extract name from child or first named child
                        name = ""
                        for child in node.children:
                            if child.type == "identifier" or child.type == "name":
                                name = child.text.decode("utf-8")
                                break
                            if child.type in ("variable_declarator", "property_identifier"):
                                for gc in child.children:
                                    if gc.type == "identifier":
                                        name = gc.text.decode("utf-8")
                                        break
                                if name:
                                    break

                        if name:
                            line = node.start_point[0] + 1
                            col = node.start_point[1]
                            symbols.append(Symbol(kind, name, line, col))

                if not cursor.goto_first_child():
                    while not cursor.goto_next_sibling():
                        if not cursor.goto_parent():
                            break
                if cursor.node == root and cursor.depth == 0:
                    break

        except Exception:
            pass  # tree-sitter failed — caller falls back to generic parser

        return symbols

    def find_references(self, symbol_name: str) -> List[dict]:
        """Find all references to a symbol across the indexed codebase.

        Returns list of dicts with keys: path, line, col, context
        """
        import re
        refs = []
        # Use base name for method references (e.g., "Class.method" -> "method")
        base_name = symbol_name.split(".")[-1]

        for path, file_idx in self.files.items():
            content = "\n".join(file_idx.lines)
            # Pattern: word boundary + symbol name + not followed by definition patterns
            pattern = re.compile(
                rf'\b{re.escape(base_name)}\b',
                re.MULTILINE
            )
            for match in pattern.finditer(content):
                # Convert absolute position to line/col
                line_start = content.rfind('\n', 0, match.start())
                line_no = content[:match.start()].count('\n') + 1
                col = match.start() - line_start - 1 if line_start >= 0 else match.start()

                # Skip self-references (the definition line)
                is_definition = False
                for sym in file_idx.symbols:
                    if sym.name == symbol_name or sym.name.endswith(f".{base_name}"):
                        if sym.line == line_no:
                            is_definition = True
                            break

                if not is_definition:
                    line_text = file_idx.lines[line_no - 1] if line_no <= len(file_idx.lines) else ""
                    refs.append({
                        "path": path,
                        "line": line_no,
                        "col": max(0, col),
                        "context": line_text.strip()[:120],
                    })

        return refs

    def build_call_graph(self) -> Dict[str, List[dict]]:
        """Build a simple call graph: symbol -> list of symbols it calls.

        Uses heuristic regex matching for cross-file references.
        """
        import re
        graph: Dict[str, List[dict]] = {}
        all_symbols = {}
        for path, file_idx in self.files.items():
            for sym in file_idx.symbols:
                if sym.kind in ("function", "method"):
                    all_symbols[sym.name] = {"path": path, "line": sym.line, "file": file_idx}

        for caller_name, caller_info in all_symbols.items():
            file_idx = caller_info["file"]
            # Get the function body by finding the next function/class
            start_line = caller_info["line"]
            end_line = len(file_idx.lines)
            for sym in file_idx.symbols:
                if sym.line > start_line and sym.kind in ("function", "method", "class"):
                    end_line = sym.line
                    break

            body = "\n".join(file_idx.lines[start_line:end_line])
            called = []
            for callee_name, callee_info in all_symbols.items():
                if callee_name == caller_name:
                    continue
                base = callee_name.split(".")[-1]
                if re.search(rf'\b{re.escape(base)}\s*\(', body):
                    called.append({
                        "name": callee_name,
                        "path": callee_info["path"],
                        "line": callee_info["line"],
                    })
            if called:
                graph[caller_name] = called

        return graph

    def get_related_symbols(self, symbol_name: str) -> List[dict]:
        """Get symbols related to the given one (same class, callers, callees)."""
        related = []

        # Same class methods
        if "." in symbol_name:
            class_name = symbol_name.split(".")[0]
            for path, file_idx in self.files.items():
                for sym in file_idx.symbols:
                    if sym.name.startswith(f"{class_name}.") and sym.name != symbol_name:
                        related.append({
                            "relation": "same_class",
                            "name": sym.name,
                            "path": path,
                            "line": sym.line,
                            "kind": sym.kind,
                        })

        # Callers
        graph = self.build_call_graph()
        for caller, callees in graph.items():
            for c in callees:
                if c["name"] == symbol_name:
                    related.append({
                        "relation": "called_by",
                        "name": caller,
                        "path": c["path"],
                        "line": c["line"],
                        "kind": "function",
                    })

        # Callees
        if symbol_name in graph:
            for c in graph[symbol_name]:
                related.append({
                    "relation": "calls",
                    "name": c["name"],
                    "path": c["path"],
                    "line": c["line"],
                    "kind": "function",
                })

        return related

    def save(self, path: str):
        """Serialize index to JSON file."""
        data = {
            "root_dir": str(self.root_dir),
            "files": {
                name: {
                    "path": fi.path,
                    "symbols": [asdict(s) for s in fi.symbols],
                    "lines": fi.lines,
                    "language": fi.language,
                }
                for name, fi in self.files.items()
            }
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def load(self, path: str):
        """Load index from JSON file."""
        if not Path(path).exists():
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        self.files = {}
        for name, fi_data in data.get("files", {}).items():
            self.files[name] = FileIndex(
                path=fi_data["path"],
                symbols=[Symbol(**s) for s in fi_data.get("symbols", [])],
                lines=fi_data.get("lines", []),
                language=fi_data.get("language", ""),
            )
