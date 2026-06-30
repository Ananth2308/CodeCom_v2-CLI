"""
File System Tool Engine for CodeCom V2.

Defines the tools that the LLM can use to interact with the user's codebase.
Each tool is:
1. Defined with its schema (name, description, parameters) in TOOL_DEFINITIONS
2. Implemented as a function that takes args + working_dir and returns a string result

Tools are split into two categories:
- Read-only: execute immediately without user approval (read_file, list_directory, search_files)
- Destructive: require human approval before execution (edit_file, append_file, write_file)

The DESTRUCTIVE_TOOLS set is used by the approval module to determine which tools need consent.
"""

import os
import re
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# TOOL DEFINITIONS (OpenAI function-calling schema format)
# These are included in the system prompt to teach the model what tools exist.
# ─────────────────────────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file with new content. Use this for targeted edits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to edit"},
                    "old_string": {"type": "string", "description": "The exact text to find and replace"},
                    "new_string": {"type": "string", "description": "The text to replace it with"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append content to the end of an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "Content to append"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or overwrite an existing file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to write"},
                    "content": {"type": "string", "description": "Content to write to the file"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List the file and directory tree structure of a given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list (default: current directory)"},
                    "max_depth": {"type": "integer", "description": "Maximum depth to traverse (default: 3)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a regex pattern across files in the codebase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in (default: current directory)"},
                    "file_pattern": {"type": "string", "description": "Glob pattern to filter files (e.g. '*.py')"},
                },
                "required": ["pattern"],
            },
        },
    },
]


# Tools that modify the file system — these require user approval before execution
DESTRUCTIVE_TOOLS = {"edit_file", "append_file", "write_file"}


# ─────────────────────────────────────────────────────────────────────────────
# TOOL EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict, working_dir: str) -> str:
    """
    Execute a tool by name with the given arguments.

    Args:
        name: Tool function name (e.g., "read_file")
        args: Dictionary of tool arguments (e.g., {"path": "main.py"})
        working_dir: Base directory for resolving relative paths

    Returns:
        String result (file contents, success message, or error message)
    """
    handlers = {
        "read_file": _read_file,
        "edit_file": _edit_file,
        "append_file": _append_file,
        "write_file": _write_file,
        "list_directory": _list_directory,
        "search_files": _search_files,
    }

    handler = handlers.get(name)
    if handler is None:
        return f"Error: Unknown tool '{name}'"

    try:
        return handler(args, working_dir)
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# HELPER: PATH RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_path(path_str: str, working_dir: str) -> Path:
    """
    Resolve a path string to an absolute Path object.
    Relative paths are resolved against the working directory.
    """
    p = Path(path_str)
    if not p.is_absolute():
        p = Path(working_dir) / p
    return p.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# TOOL IMPLEMENTATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _read_file(args: dict, working_dir: str) -> str:
    """Read and return the contents of a file. Truncates at 100KB to avoid memory issues."""
    path = _resolve_path(args["path"], working_dir)
    if not path.exists():
        return f"Error: File not found: {path}"
    if not path.is_file():
        return f"Error: Not a file: {path}"
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: Cannot read binary file: {path}"
    # Truncate very large files to prevent context overflow
    if len(content) > 100_000:
        return f"[File truncated - showing first 100000 chars]\n{content[:100000]}"
    return content


def _edit_file(args: dict, working_dir: str) -> str:
    """
    Find-and-replace a specific string in a file.
    Requires the old_string to appear exactly once (for safety).
    """
    path = _resolve_path(args["path"], working_dir)
    if not path.exists():
        return f"Error: File not found: {path}"
    content = path.read_text(encoding="utf-8")
    old_string = args["old_string"]
    new_string = args["new_string"]
    # Verify the target text exists
    if old_string not in content:
        return f"Error: Could not find the specified text in {path}"
    # Safety check: require unique match to prevent unintended replacements
    count = content.count(old_string)
    if count > 1:
        return f"Error: Found {count} occurrences of the text. Please provide more context to make it unique."
    # Perform the replacement
    new_content = content.replace(old_string, new_string, 1)
    path.write_text(new_content, encoding="utf-8")
    return f"Successfully edited {path}"


def _append_file(args: dict, working_dir: str) -> str:
    """Append content to the end of an existing file."""
    path = _resolve_path(args["path"], working_dir)
    if not path.exists():
        return f"Error: File not found: {path}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(args["content"])
    return f"Successfully appended to {path}"


def _write_file(args: dict, working_dir: str) -> str:
    """Create a new file or overwrite an existing file. Creates parent directories if needed."""
    path = _resolve_path(args["path"], working_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return f"Successfully wrote {path}"


def _list_directory(args: dict, working_dir: str) -> str:
    """
    List directory contents as a tree structure.
    Skips common non-essential directories (.git, node_modules, __pycache__, etc.)
    Limits output to 500 lines to prevent context overflow.
    """
    path = _resolve_path(args.get("path", "."), working_dir)
    max_depth = args.get("max_depth", 3)
    # Handle string max_depth (from text-based tool calls)
    if isinstance(max_depth, str):
        try:
            max_depth = int(max_depth)
        except ValueError:
            max_depth = 3
    if not path.exists():
        return f"Error: Directory not found: {path}"
    if not path.is_dir():
        return f"Error: Not a directory: {path}"

    lines = []
    _tree(path, "", max_depth, 0, lines)
    if not lines:
        return "(empty directory)"
    # Cap output to prevent overwhelming the model's context
    return "\n".join(lines[:500])


def _tree(path: Path, prefix: str, max_depth: int, current_depth: int, lines: list):
    """Recursively build a tree representation of a directory."""
    if current_depth >= max_depth:
        return

    # Directories to skip (not useful for code understanding, and often huge)
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}
    try:
        entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        lines.append(f"{prefix}[Permission Denied]")
        return

    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        if entry.is_dir():
            if entry.name in skip_dirs:
                lines.append(f"{prefix}{connector}{entry.name}/ (skipped)")
                continue
            lines.append(f"{prefix}{connector}{entry.name}/")
            extension = "    " if is_last else "│   "
            _tree(entry, prefix + extension, max_depth, current_depth + 1, lines)
        else:
            lines.append(f"{prefix}{connector}{entry.name}")


def _search_files(args: dict, working_dir: str) -> str:
    """
    Search for a regex pattern across files in the codebase.
    Similar to 'grep -r'. Returns matching lines with file:line_number: prefix.
    Limits: 5000 files scanned, 100 matches returned.
    """
    pattern = args["pattern"]
    search_path = _resolve_path(args.get("path", "."), working_dir)
    file_pattern = args.get("file_pattern", "*")

    if not search_path.exists():
        return f"Error: Path not found: {search_path}"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"

    results = []
    files_searched = 0

    for filepath in search_path.rglob(file_pattern):
        if not filepath.is_file():
            continue
        # Skip hidden directories and common non-source directories
        if any(part.startswith(".") or part in {"node_modules", "__pycache__", "venv", ".venv"}
               for part in filepath.parts):
            continue
        files_searched += 1
        # Safety limit: don't scan the entire filesystem
        if files_searched > 5000:
            results.append("... (stopped after searching 5000 files)")
            break
        try:
            content = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                rel = filepath.relative_to(search_path)
                results.append(f"{rel}:{i}: {line.strip()}")
                if len(results) > 100:
                    results.append("... (truncated at 100 matches)")
                    return "\n".join(results)

    if not results:
        return f"No matches found for '{pattern}' in {files_searched} files."
    return "\n".join(results)
