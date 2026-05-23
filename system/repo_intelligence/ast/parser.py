"""AST Parser using tree-sitter to extract semantic code chunks.

Supports Python and TypeScript/JavaScript source files.  Each parsed file
is decomposed into CodeChunk records covering functions, classes, methods,
import groups, and the module-level docstring.
"""

from __future__ import annotations

import hashlib
import os
import textwrap
from typing import List, Optional, Tuple

from system.observability.logging.logger import get_logger
from system.repo_intelligence.schemas import CodeChunk
from system.shared.exceptions import RepoIntelligenceError

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Tree-sitter lazy initialisation helpers
# ---------------------------------------------------------------------------

_PYTHON_PARSER = None
_TS_PARSER = None
_JS_PARSER = None


def _get_python_parser():
    global _PYTHON_PARSER
    if _PYTHON_PARSER is None:
        try:
            import tree_sitter_python as tspython
            from tree_sitter import Language, Parser

            PY_LANGUAGE = Language(tspython.language())
            _PYTHON_PARSER = Parser(PY_LANGUAGE)
        except Exception as exc:
            raise RepoIntelligenceError(
                f"Failed to initialise tree-sitter Python parser: {exc}"
            )
    return _PYTHON_PARSER


def _get_ts_parser():
    global _TS_PARSER
    if _TS_PARSER is None:
        try:
            import tree_sitter_typescript as tsts
            from tree_sitter import Language, Parser

            TS_LANGUAGE = Language(tsts.language_typescript())
            _TS_PARSER = Parser(TS_LANGUAGE)
        except Exception as exc:
            raise RepoIntelligenceError(
                f"Failed to initialise tree-sitter TypeScript parser: {exc}"
            )
    return _TS_PARSER


def _get_js_parser():
    global _JS_PARSER
    if _JS_PARSER is None:
        try:
            import tree_sitter_javascript as tsjs
            from tree_sitter import Language, Parser

            JS_LANGUAGE = Language(tsjs.language())
            _JS_PARSER = Parser(JS_LANGUAGE)
        except Exception as exc:
            raise RepoIntelligenceError(
                f"Failed to initialise tree-sitter JavaScript parser: {exc}"
            )
    return _JS_PARSER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk_id(file_path: str, name: str, start_line: int) -> str:
    raw = f"{file_path}:{name}:{start_line}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _node_text(node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_lines(node) -> Tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _find_docstring(node, source_bytes: bytes) -> Optional[str]:
    """Return docstring of a function/class body, or None."""
    for child in node.children:
        if child.type in ("block", "suite", "statement_block"):
            for stmt in child.children:
                if stmt.type == "expression_statement":
                    for inner in stmt.children:
                        if inner.type == "string":
                            raw = _node_text(inner, source_bytes)
                            return textwrap.dedent(
                                raw.strip("\"'").strip('"""').strip("'''").strip()
                            )
            break
    return None


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------


class ASTParser:
    """Parse source files into semantic CodeChunk objects via tree-sitter."""

    def __init__(self) -> None:
        # Parsers are initialised lazily on first use
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_file(self, file_path: str, project_id: str) -> List[CodeChunk]:
        """Parse *file_path* and return a list of CodeChunk records.

        Args:
            file_path: Absolute path to the source file.
            project_id: Project this file belongs to.

        Returns:
            A list of CodeChunk objects covering the file's semantic units.

        Raises:
            RepoIntelligenceError: If the file cannot be read or parsed.
        """
        ext = os.path.splitext(file_path)[1].lower()
        language_map = {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
        }
        language = language_map.get(ext)
        if language is None:
            logger.debug("Skipping unsupported file: %s", file_path)
            return []

        try:
            with open(file_path, "rb") as fh:
                source_bytes = fh.read()
        except OSError as exc:
            raise RepoIntelligenceError(
                f"Cannot read file {file_path}: {exc}",
                details={"file_path": file_path},
            )

        source_str = source_bytes.decode("utf-8", errors="replace")

        try:
            if language == "python":
                return self._parse_python(source_str, source_bytes, file_path, project_id)
            else:
                return self._parse_typescript(source_str, source_bytes, file_path, project_id, language)
        except RepoIntelligenceError:
            raise
        except Exception as exc:
            raise RepoIntelligenceError(
                f"AST parsing failed for {file_path}: {exc}",
                details={"file_path": file_path, "language": language},
            )

    # ------------------------------------------------------------------
    # Python parser
    # ------------------------------------------------------------------

    def _parse_python(
        self,
        source: str,
        source_bytes: bytes,
        file_path: str,
        project_id: str,
    ) -> List[CodeChunk]:
        parser = _get_python_parser()
        tree = parser.parse(source_bytes)
        root = tree.root_node

        chunks: List[CodeChunk] = []
        imports = self._extract_imports(root, source_bytes, "python")
        exports = self._extract_exports(root, source_bytes, "python")

        # Module-level docstring
        module_doc = self._get_module_docstring_python(root, source_bytes)
        lines = source.splitlines()
        if module_doc:
            chunk = CodeChunk(
                chunk_id=_make_chunk_id(file_path, "__module_doc__", 1),
                file_path=file_path,
                chunk_type="module",
                name=os.path.basename(file_path),
                content=module_doc,
                start_line=1,
                end_line=min(10, len(lines)),
                language="python",
                docstring=module_doc,
                dependencies=imports,
                exports=exports,
                project_id=project_id,
            )
            chunks.append(chunk)

        for node in root.children:
            if node.type == "function_definition":
                fn_chunks = self._extract_python_function(
                    node, source_bytes, file_path, project_id, imports, exports
                )
                for fc in fn_chunks:
                    chunks.extend(self._chunk_large_function(fc))

            elif node.type == "class_definition":
                cls_chunks = self._extract_python_class(
                    node, source_bytes, file_path, project_id, imports, exports
                )
                chunks.extend(cls_chunks)

            elif node.type in ("import_statement", "import_from_statement"):
                # Group all imports as a single import chunk at the top
                pass  # Already captured in module-level imports list

        # Add standalone import chunk if file has imports
        if imports:
            import_lines = [
                l for l in lines if l.startswith("import ") or l.startswith("from ")
            ]
            if import_lines:
                end_ln = min(len(import_lines) + 1, len(lines))
                chunks.append(
                    CodeChunk(
                        chunk_id=_make_chunk_id(file_path, "__imports__", 1),
                        file_path=file_path,
                        chunk_type="import",
                        name="imports",
                        content="\n".join(import_lines),
                        start_line=1,
                        end_line=end_ln,
                        language="python",
                        dependencies=imports,
                        exports=[],
                        project_id=project_id,
                    )
                )

        return chunks

    def _get_module_docstring_python(self, root, source_bytes: bytes) -> Optional[str]:
        for child in root.children:
            if child.type == "expression_statement":
                for inner in child.children:
                    if inner.type == "string":
                        raw = _node_text(inner, source_bytes)
                        return raw.strip('"\'').strip()
            elif child.type not in ("comment", "newline"):
                break
        return None

    def _extract_python_function(
        self,
        node,
        source_bytes: bytes,
        file_path: str,
        project_id: str,
        imports: List[str],
        exports: List[str],
    ) -> List[CodeChunk]:
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, source_bytes) if name_node else "unknown"
        start_line, end_line = _node_lines(node)
        content = _node_text(node, source_bytes)
        docstring = _find_docstring(node, source_bytes)

        return [
            CodeChunk(
                chunk_id=_make_chunk_id(file_path, name, start_line),
                file_path=file_path,
                chunk_type="function",
                name=name,
                content=content,
                start_line=start_line,
                end_line=end_line,
                language="python",
                docstring=docstring,
                dependencies=imports,
                exports=exports,
                project_id=project_id,
            )
        ]

    def _extract_python_class(
        self,
        node,
        source_bytes: bytes,
        file_path: str,
        project_id: str,
        imports: List[str],
        exports: List[str],
    ) -> List[CodeChunk]:
        chunks: List[CodeChunk] = []
        name_node = node.child_by_field_name("name")
        class_name = _node_text(name_node, source_bytes) if name_node else "Unknown"
        start_line, end_line = _node_lines(node)
        content = _node_text(node, source_bytes)
        docstring = _find_docstring(node, source_bytes)

        # Class-level chunk (full class or header)
        chunks.append(
            CodeChunk(
                chunk_id=_make_chunk_id(file_path, class_name, start_line),
                file_path=file_path,
                chunk_type="class",
                name=class_name,
                content=content,
                start_line=start_line,
                end_line=end_line,
                language="python",
                docstring=docstring,
                dependencies=imports,
                exports=exports,
                project_id=project_id,
            )
        )

        # Extract individual methods
        body_node = node.child_by_field_name("body")
        if body_node:
            for child in body_node.children:
                if child.type == "function_definition":
                    method_name_node = child.child_by_field_name("name")
                    method_name = (
                        _node_text(method_name_node, source_bytes)
                        if method_name_node
                        else "unknown"
                    )
                    m_start, m_end = _node_lines(child)
                    method_content = _node_text(child, source_bytes)
                    method_doc = _find_docstring(child, source_bytes)
                    method_chunk = CodeChunk(
                        chunk_id=_make_chunk_id(
                            file_path, f"{class_name}.{method_name}", m_start
                        ),
                        file_path=file_path,
                        chunk_type="method",
                        name=f"{class_name}.{method_name}",
                        content=method_content,
                        start_line=m_start,
                        end_line=m_end,
                        language="python",
                        docstring=method_doc,
                        dependencies=imports,
                        exports=[],
                        project_id=project_id,
                    )
                    chunks.extend(self._chunk_large_function(method_chunk))

        return chunks

    # ------------------------------------------------------------------
    # TypeScript / JavaScript parser
    # ------------------------------------------------------------------

    def _parse_typescript(
        self,
        source: str,
        source_bytes: bytes,
        file_path: str,
        project_id: str,
        language: str,
    ) -> List[CodeChunk]:
        if language in ("typescript",):
            parser = _get_ts_parser()
        else:
            parser = _get_js_parser()

        tree = parser.parse(source_bytes)
        root = tree.root_node

        chunks: List[CodeChunk] = []
        imports = self._extract_imports(root, source_bytes, language)
        exports = self._extract_exports(root, source_bytes, language)

        if imports:
            import_lines = [
                l.strip()
                for l in source.splitlines()
                if l.strip().startswith("import ")
            ]
            if import_lines:
                chunks.append(
                    CodeChunk(
                        chunk_id=_make_chunk_id(file_path, "__imports__", 1),
                        file_path=file_path,
                        chunk_type="import",
                        name="imports",
                        content="\n".join(import_lines),
                        start_line=1,
                        end_line=len(import_lines),
                        language=language,
                        dependencies=imports,
                        exports=[],
                        project_id=project_id,
                    )
                )

        TS_FUNCTION_TYPES = {
            "function_declaration",
            "arrow_function",
            "function_expression",
            "method_definition",
            "generator_function_declaration",
        }
        TS_CLASS_TYPES = {"class_declaration", "class"}
        TS_INTERFACE_TYPES = {"interface_declaration"}
        TS_TYPE_ALIAS = {"type_alias_declaration"}

        def visit(node, parent_class: Optional[str] = None):
            if node.type in TS_FUNCTION_TYPES:
                name = self._ts_node_name(node, source_bytes, parent_class)
                start_line, end_line = _node_lines(node)
                content = _node_text(node, source_bytes)
                chunk = CodeChunk(
                    chunk_id=_make_chunk_id(file_path, name, start_line),
                    file_path=file_path,
                    chunk_type="method" if parent_class else "function",
                    name=name,
                    content=content,
                    start_line=start_line,
                    end_line=end_line,
                    language=language,
                    dependencies=imports,
                    exports=exports if not parent_class else [],
                    project_id=project_id,
                )
                chunks.extend(self._chunk_large_function(chunk))

            elif node.type in TS_CLASS_TYPES:
                name_node = node.child_by_field_name("name")
                class_name = (
                    _node_text(name_node, source_bytes) if name_node else "Anonymous"
                )
                start_line, end_line = _node_lines(node)
                content = _node_text(node, source_bytes)
                chunks.append(
                    CodeChunk(
                        chunk_id=_make_chunk_id(file_path, class_name, start_line),
                        file_path=file_path,
                        chunk_type="class",
                        name=class_name,
                        content=content,
                        start_line=start_line,
                        end_line=end_line,
                        language=language,
                        dependencies=imports,
                        exports=exports,
                        project_id=project_id,
                    )
                )
                for child in node.children:
                    visit(child, parent_class=class_name)
                return  # avoid double-visiting children below

            elif node.type in TS_INTERFACE_TYPES:
                name_node = node.child_by_field_name("name")
                iface_name = (
                    _node_text(name_node, source_bytes) if name_node else "Interface"
                )
                start_line, end_line = _node_lines(node)
                chunks.append(
                    CodeChunk(
                        chunk_id=_make_chunk_id(file_path, iface_name, start_line),
                        file_path=file_path,
                        chunk_type="class",  # treat interface as class-like
                        name=iface_name,
                        content=_node_text(node, source_bytes),
                        start_line=start_line,
                        end_line=end_line,
                        language=language,
                        dependencies=imports,
                        exports=exports,
                        project_id=project_id,
                    )
                )

            elif node.type in TS_TYPE_ALIAS:
                name_node = node.child_by_field_name("name")
                type_name = (
                    _node_text(name_node, source_bytes) if name_node else "Type"
                )
                start_line, end_line = _node_lines(node)
                chunks.append(
                    CodeChunk(
                        chunk_id=_make_chunk_id(file_path, type_name, start_line),
                        file_path=file_path,
                        chunk_type="module",
                        name=type_name,
                        content=_node_text(node, source_bytes),
                        start_line=start_line,
                        end_line=end_line,
                        language=language,
                        dependencies=imports,
                        exports=exports,
                        project_id=project_id,
                    )
                )

            for child in node.children:
                if node.type not in TS_CLASS_TYPES:
                    visit(child, parent_class)

        for child in root.children:
            visit(child)

        return chunks

    def _ts_node_name(
        self, node, source_bytes: bytes, parent_class: Optional[str]
    ) -> str:
        name_node = node.child_by_field_name("name")
        if name_node:
            base = _node_text(name_node, source_bytes)
            return f"{parent_class}.{base}" if parent_class else base
        return f"{parent_class}.anonymous" if parent_class else "anonymous"

    # ------------------------------------------------------------------
    # Import / export extraction
    # ------------------------------------------------------------------

    def _extract_imports(self, root, source_bytes: bytes, language: str) -> List[str]:
        imports: List[str] = []
        if language == "python":
            for node in self._walk(root):
                if node.type == "import_statement":
                    for child in node.children:
                        if child.type == "dotted_name":
                            imports.append(_node_text(child, source_bytes))
                elif node.type == "import_from_statement":
                    for child in node.children:
                        if child.type == "dotted_name":
                            imports.append(_node_text(child, source_bytes))
                            break
        else:
            for node in self._walk(root):
                if node.type == "import_statement":
                    src_node = node.child_by_field_name("source")
                    if src_node:
                        raw = _node_text(src_node, source_bytes).strip("\"'`")
                        imports.append(raw)
                elif node.type == "import_from_clause":
                    src_node = node.child_by_field_name("source")
                    if src_node:
                        raw = _node_text(src_node, source_bytes).strip("\"'`")
                        imports.append(raw)
        return list(dict.fromkeys(imports))  # deduplicate, preserve order

    def _extract_exports(self, root, source_bytes: bytes, language: str) -> List[str]:
        exports: List[str] = []
        if language == "python":
            for node in self._walk(root):
                if node.type == "assignment":
                    target = node.child_by_field_name("left")
                    if target and _node_text(target, source_bytes) == "__all__":
                        val = node.child_by_field_name("right")
                        if val:
                            for item in val.children:
                                if item.type == "string":
                                    exports.append(
                                        _node_text(item, source_bytes).strip("\"'")
                                    )
        else:
            for node in self._walk(root):
                if node.type in (
                    "export_statement",
                    "export_default_declaration",
                ):
                    dec = node.child_by_field_name("declaration")
                    if dec:
                        name_node = dec.child_by_field_name("name")
                        if name_node:
                            exports.append(_node_text(name_node, source_bytes))
        return list(dict.fromkeys(exports))

    # ------------------------------------------------------------------
    # Large-function splitter
    # ------------------------------------------------------------------

    def _chunk_large_function(
        self, chunk: CodeChunk, max_lines: int = 50
    ) -> List[CodeChunk]:
        """Split a function chunk that exceeds *max_lines* into sub-chunks."""
        lines = chunk.content.splitlines()
        if len(lines) <= max_lines:
            return [chunk]

        sub_chunks: List[CodeChunk] = []
        overlap = 5
        part = 0
        i = 0
        while i < len(lines):
            end = min(i + max_lines, len(lines))
            sub_content = "\n".join(lines[i:end])
            sub_start = chunk.start_line + i
            sub_end = chunk.start_line + end - 1
            sub_chunks.append(
                CodeChunk(
                    chunk_id=_make_chunk_id(
                        chunk.file_path, f"{chunk.name}__part{part}", sub_start
                    ),
                    file_path=chunk.file_path,
                    chunk_type=chunk.chunk_type,
                    name=f"{chunk.name}__part{part}",
                    content=sub_content,
                    start_line=sub_start,
                    end_line=sub_end,
                    language=chunk.language,
                    docstring=chunk.docstring if part == 0 else None,
                    dependencies=chunk.dependencies,
                    exports=chunk.exports if part == 0 else [],
                    project_id=chunk.project_id,
                )
            )
            part += 1
            i = end - overlap if end < len(lines) else end
        return sub_chunks

    # ------------------------------------------------------------------
    # Tree walker
    # ------------------------------------------------------------------

    def _walk(self, node):
        """Depth-first node iterator."""
        yield node
        for child in node.children:
            yield from self._walk(child)
