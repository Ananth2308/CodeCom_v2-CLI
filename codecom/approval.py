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
from rich.console import Console
from rich.panel import Panel
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
    - edit_file: shows the file path and a diff-style view (old → new)
    - write_file: shows the file path and content preview
    - append_file: shows the file path and what's being appended
    """
    if tool_name == "edit_file":
        path = args.get("path", "?")
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        return (
            f"[bold]File:[/bold] {path}\n\n"
            f"[red]- {old}[/red]\n"
            f"[green]+ {new}[/green]"
        )
    elif tool_name == "write_file":
        path = args.get("path", "?")
        content = args.get("content", "")
        # Only show first 500 chars to keep the panel manageable
        preview = content[:500]
        if len(content) > 500:
            preview += "\n... (truncated)"
        return f"[bold]File:[/bold] {path}\n[bold]Content:[/bold]\n{preview}"
    elif tool_name == "append_file":
        path = args.get("path", "?")
        content = args.get("content", "")
        preview = content[:300]
        if len(content) > 300:
            preview += "\n... (truncated)"
        return f"[bold]File:[/bold] {path}\n[bold]Appending:[/bold]\n{preview}"
    else:
        # Fallback: just dump the raw args as JSON
        return json.dumps(args, indent=2)
