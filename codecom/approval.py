"""
Human-in-the-Loop Approval System for CodeCom V2.

This module implements the safety gate between the LLM's intent to modify files
and the actual execution of that modification. It ensures that no destructive
action (edit, write, append) happens without explicit user consent.

Approval flow:
1. The model requests a destructive tool call (e.g., edit_file)
2. This module shows a rich panel with the action details
3. The user is prompted: [y]es / [n]o / [a]lways
4. If approved, the tool executes. If denied, the model is told "Action denied."

The "always" option disables future prompts for that specific tool type
(within the current session only — restarting CodeCom resets this).
"""

import json
import difflib
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML

from codecom.tools import DESTRUCTIVE_TOOLS

console = Console()


def request_approval(tool_name: str, args: dict) -> bool:
    """
    Ask the user for approval before executing a destructive tool.

    Non-destructive tools (read_file, list_directory, search_files) are
    automatically approved and never reach this function.

    Args:
        tool_name: Name of the tool being called (e.g., "edit_file")
        args: Tool arguments dict (e.g., {"path": "main.py", "old_string": "...", "new_string": "..."})

    Returns:
        True if the user approves the action, False if denied.
    """
    # Read-only tools don't need approval (this is a safety check in case
    # this function is called for non-destructive tools)
    if tool_name not in DESTRUCTIVE_TOOLS:
        return True

    # Display the action details in a colored panel
    console.print()
    console.print(Panel(
        _format_action(tool_name, args),
        title=f"[bold yellow]⚠ Approval Required: {tool_name}[/bold yellow]",
        border_style="yellow",
    ))

    # Prompt loop until we get a valid answer
    while True:
        try:
            answer = prompt(
                HTML("<b>[y]es / [n]o / [a]lways: </b>"),
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            # Ctrl+C or Ctrl+D = deny
            return False

        if answer in ("y", "yes"):
            return True
        elif answer in ("n", "no"):
            return False
        elif answer in ("a", "always"):
            # Remove this tool from the destructive set for the rest of the session
            # This means future calls to this tool type won't prompt anymore
            DESTRUCTIVE_TOOLS.discard(tool_name)
            return True
        else:
            console.print("[dim]Please enter y, n, or a[/dim]")


def _format_action(tool_name: str, args: dict) -> str:
    """
    Format a tool action for display in the approval panel.
    Shows the relevant details based on tool type:
    - edit_file: shows the file path and a unified diff with syntax highlighting
    - write_file: shows the file path and content preview with syntax highlighting
    - append_file: shows the file path and what's being appended
    - delete_file: shows the file path being deleted
    - rename_file: shows old path -> new path
    - git_commit: shows commit message and files
    """
    if tool_name == "edit_file":
        path = args.get("path", "?")
        old = args.get("old_string", "")
        new = args.get("new_string", "")

        # Create a unified diff for better visualization
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{path} (before)",
            tofile=f"{path} (after)",
            lineterm=""
        ))

        if diff:
            # Format the diff with colors: red for removed, green for added
            formatted_lines = []
            for line in diff:
                if line.startswith("---") or line.startswith("+++"):
                    # File headers - make them dim
                    formatted_lines.append(f"[dim]{line}[/dim]")
                elif line.startswith("@@"):
                    # Hunk headers - make them blue
                    formatted_lines.append(f"[blue]{line}[/blue]")
                elif line.startswith("-") and not line.startswith("---"):
                    # Removed lines - red
                    formatted_lines.append(f"[red]{line}[/red]")
                elif line.startswith("+") and not line.startswith("+++"):
                    # Added lines - green
                    formatted_lines.append(f"[green]{line}[/green]")
                else:
                    # Context lines
                    formatted_lines.append(line)

            diff_text = "\n".join(formatted_lines)
            # Limit diff size
            if len(diff_text) > 2000:
                diff_text = diff_text[:2000] + "\n[dim]... (diff truncated)[/dim]"
        else:
            diff_text = f"[red]- {old}[/red]\n[green]+ {new}[/green]"

        return f"[bold]File:[/bold] {path}\n\n{diff_text}"

    elif tool_name == "write_file":
        path = args.get("path", "?")
        content = args.get("content", "")

        # Detect file extension for syntax highlighting
        ext = path.split(".")[-1] if "." in path else "txt"
        lexer_map = {
            "py": "python", "js": "javascript", "ts": "typescript",
            "java": "java", "cpp": "cpp", "c": "c", "go": "go",
            "rs": "rust", "rb": "ruby", "php": "php", "html": "html",
            "css": "css", "json": "json", "yaml": "yaml", "yml": "yaml",
            "xml": "xml", "md": "markdown", "sh": "bash", "sql": "sql"
        }
        lexer = lexer_map.get(ext, "text")

        # Truncate content for preview
        preview = content[:800]
        truncated = len(content) > 800

        result = f"[bold]File:[/bold] {path}\n[bold]Content:[/bold] ({len(content)} chars)\n\n"
        result += preview
        if truncated:
            result += "\n\n... (content truncated, full content will be written)"

        return result

    elif tool_name == "append_file":
        path = args.get("path", "?")
        content = args.get("content", "")
        preview = content[:400]
        if len(content) > 400:
            preview += "\n... (truncated)"
        return f"[bold]File:[/bold] {path}\n[bold]Appending:[/bold] ({len(content)} chars)\n\n{preview}"

    elif tool_name == "delete_file":
        path = args.get("path", "?")
        return f"[bold red]⚠ DELETE FILE:[/bold red] {path}\n\n[yellow]This action cannot be undone![/yellow]"

    elif tool_name == "rename_file":
        old_path = args.get("old_path", "?")
        new_path = args.get("new_path", "?")
        return f"[bold]Rename/Move:[/bold]\n[red]From:[/red] {old_path}\n[green]To:[/green]   {new_path}"

    elif tool_name == "git_commit":
        message = args.get("message", "?")
        files = args.get("files", "all staged files")
        return f"[bold]Commit Message:[/bold]\n{message}\n\n[bold]Files:[/bold] {files}"

    else:
        # Fallback: just dump the raw args as JSON
        return json.dumps(args, indent=2)
