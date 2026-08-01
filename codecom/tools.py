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
import subprocess
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
                    "case_insensitive": {"type": "boolean", "description": "Ignore case when matching (default: false)"},
                    "multiline": {"type": "boolean", "description": "Enable multiline mode - ^ and $ match line boundaries (default: false)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to delete"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "Rename or move a file to a new location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {"type": "string", "description": "Current path of the file"},
                    "new_path": {"type": "string", "description": "New path for the file"},
                },
                "required": ["old_path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show the working tree status of the git repository.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show changes in the working tree or staged changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Specific file or directory to diff (optional)"},
                    "staged": {"type": "boolean", "description": "Show staged changes instead of unstaged (default: false)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show commit history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_count": {"type": "integer", "description": "Maximum number of commits to show (default: 10)"},
                    "path": {"type": "string", "description": "Show commits only for a specific file or directory (optional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Create a git commit with staged or specified files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                    "files": {"type": "string", "description": "Comma-separated list of files to stage and commit (optional, uses already staged files if not provided)"},
                },
                "required": ["message"],
            },
        },
    },
]


# Tools that modify the file system or git history — these require user approval before execution
DESTRUCTIVE_TOOLS = {"edit_file", "append_file", "write_file", "delete_file", "rename_file", "git_commit"}


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
        "delete_file": _delete_file,
        "rename_file": _rename_file,
        "git_status": _git_status,
        "git_diff": _git_diff,
        "git_log": _git_log,
        "git_commit": _git_commit,
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
    """Read and return the contents of a file with line numbers. Truncates at 100KB to avoid memory issues."""
    path = _resolve_path(args["path"], working_dir)
    if not path.exists():
        return f"Error: File not found: {path}"
    if not path.is_file():
        return f"Error: Not a file: {path}"
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: Cannot read binary file: {path}"

    # Add line numbers to the content
    lines = content.splitlines()
    max_line_num = len(lines)
    width = len(str(max_line_num))

    # Truncate very large files to prevent context overflow
    if len(content) > 100_000:
        truncate_at_line = len(content[:100000].splitlines())
        lines = lines[:truncate_at_line]
        numbered_lines = [f"{str(i+1).rjust(width)} | {line}" for i, line in enumerate(lines)]
        return f"[File truncated - showing first {truncate_at_line} lines]\n" + "\n".join(numbered_lines)

    numbered_lines = [f"{str(i+1).rjust(width)} | {line}" for i, line in enumerate(lines)]
    return "\n".join(numbered_lines)


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


def _delete_file(args: dict, working_dir: str) -> str:
    """Delete a file from the filesystem."""
    path = _resolve_path(args["path"], working_dir)
    if not path.exists():
        return f"Error: File not found: {path}"
    if not path.is_file():
        return f"Error: Not a file (use a different tool for directories): {path}"
    path.unlink()
    return f"Successfully deleted {path}"


def _rename_file(args: dict, working_dir: str) -> str:
    """Rename or move a file to a new location."""
    old_path = _resolve_path(args["old_path"], working_dir)
    new_path = _resolve_path(args["new_path"], working_dir)
    if not old_path.exists():
        return f"Error: File not found: {old_path}"
    if not old_path.is_file():
        return f"Error: Not a file: {old_path}"
    if new_path.exists():
        return f"Error: Destination already exists: {new_path}"
    # Create parent directories if needed
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    return f"Successfully renamed {old_path} to {new_path}"


def _search_files(args: dict, working_dir: str) -> str:
    """
    Search for a regex pattern across files in the codebase.
    Similar to 'grep -r'. Returns matching lines with file:line_number: prefix.
    Limits: 5000 files scanned, 100 matches returned.
    """
    pattern = args["pattern"]
    search_path = _resolve_path(args.get("path", "."), working_dir)
    file_pattern = args.get("file_pattern", "*")
    case_insensitive = args.get("case_insensitive", False)
    multiline = args.get("multiline", False)

    # Handle string booleans (from text-based tool calls)
    if isinstance(case_insensitive, str):
        case_insensitive = case_insensitive.lower() in ("true", "yes", "1")
    if isinstance(multiline, str):
        multiline = multiline.lower() in ("true", "yes", "1")

    if not search_path.exists():
        return f"Error: Path not found: {search_path}"

    # Build regex flags
    flags = 0
    if case_insensitive:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE

    try:
        regex = re.compile(pattern, flags)
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


# ─────────────────────────────────────────────────────────────────────────────
# GIT TOOLS
# ─────────────────────────────────────────────────────────────────────────────

def _run_git_command(args: list, cwd: str) -> tuple[bool, str]:
    """
    Helper to run a git command and return (success, output).

    Args:
        args: Git command arguments (e.g., ["status", "--short"])
        cwd: Working directory to run the command in

    Returns:
        Tuple of (success: bool, output: str)
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, f"Git error: {result.stderr.strip() or result.stdout.strip()}"
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "Error: Git command timed out after 30 seconds"
    except FileNotFoundError:
        return False, "Error: Git is not installed or not in PATH"
    except Exception as e:
        return False, f"Error running git: {type(e).__name__}: {e}"


def _git_status(args: dict, working_dir: str) -> str:
    """Show the git working tree status."""
    success, output = _run_git_command(["status", "--short", "--branch"], working_dir)
    if not success:
        return output
    if not output:
        return "Working tree is clean (no changes)"
    return output


def _git_diff(args: dict, working_dir: str) -> str:
    """Show git diff of changes."""
    path = args.get("path", "")
    staged = args.get("staged", False)

    # Handle string booleans
    if isinstance(staged, str):
        staged = staged.lower() in ("true", "yes", "1")

    cmd = ["diff"]
    if staged:
        cmd.append("--cached")
    if path:
        cmd.append(path)

    success, output = _run_git_command(cmd, working_dir)
    if not success:
        return output
    if not output:
        return "No changes to show" if not staged else "No staged changes"

    # Limit output to prevent context overflow
    if len(output) > 50_000:
        return f"[Diff truncated - showing first 50000 chars]\n{output[:50000]}"
    return output


def _git_log(args: dict, working_dir: str) -> str:
    """Show commit history."""
    max_count = args.get("max_count", 10)
    path = args.get("path", "")

    # Handle string max_count
    if isinstance(max_count, str):
        try:
            max_count = int(max_count)
        except ValueError:
            max_count = 10

    cmd = ["log", f"--max-count={max_count}", "--oneline", "--decorate", "--graph"]
    if path:
        cmd.extend(["--", path])

    success, output = _run_git_command(cmd, working_dir)
    if not success:
        return output
    if not output:
        return "No commits found"
    return output


def _git_commit(args: dict, working_dir: str) -> str:
    """Create a git commit."""
    message = args["message"]
    files = args.get("files", "")

    # Stage specific files if provided
    if files:
        file_list = [f.strip() for f in files.split(",")]
        for file in file_list:
            success, output = _run_git_command(["add", file], working_dir)
            if not success:
                return f"Error staging {file}: {output}"

    # Create the commit
    success, output = _run_git_command(["commit", "-m", message], working_dir)
    if not success:
        return output

    return f"Commit created successfully:\n{output}"
