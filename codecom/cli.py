"""
Main CLI Interface for CodeCom V2.

This module implements the interactive REPL (Read-Eval-Print Loop) that provides
the terminal UI. It handles:
- User input with history and auto-suggest
- Sending messages to the LLM via the client
- Parsing and executing tool calls from the model's responses
- Feeding tool results back to the model for continued reasoning
- The agentic loop: model → tool call → result → model → ... → final answer

The response loop continues until either:
- The model responds with plain text (no tool calls) — displayed as the final answer
- Maximum tool rounds (15) are reached — safety limit against infinite loops
- An API error occurs
"""

import os
import sys
import json
import re
import argparse
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.live import Live
from rich.text import Text
from prompt_toolkit import prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

from codecom.config import load_config
from codecom.llm_client import LLMClient
from codecom.tools import execute_tool, DESTRUCTIVE_TOOLS
from codecom.approval import request_approval

console = Console()

# Persistent command history file (survives between sessions)
HISTORY_FILE = Path.home() / ".codecom_history"


def clean_tool_call_tags(text: str) -> str:
    """
    Remove tool call XML tags from text to keep output clean.
    Removes patterns like:
    - <tool_call>
    - </tool_call>
    - <function=read_file>
    - </function>
    - <parameter=path>value</parameter>
    """
    # Remove tool_call tags
    text = re.sub(r'</?tool_call>', '', text)
    # Remove function tags with attributes
    text = re.sub(r'<function=[^>]+>', '', text)
    text = re.sub(r'</function>', '', text)
    # Remove parameter tags
    text = re.sub(r'<parameter=[^>]+>', '', text)
    text = re.sub(r'</parameter>', '', text)
    return text


def make_paths_clickable(text: str, working_dir: str) -> str:
    """
    Convert file paths in text to clickable file:// links using OSC 8 hyperlinks.
    These are invisible - the path looks normal but is clickable.
    Detects patterns like:
    - path/to/file.py
    - path/to/file.py:123
    - codecom/cli.py:45
    """
    # Pattern to match file paths (with optional line numbers)
    # Matches: word/word.ext or word/word.ext:123
    pattern = r'\b([\w/\\.-]+\.(py|js|ts|java|cpp|c|go|rs|rb|php|html|css|json|yaml|yml|xml|md|txt|sh|sql))(?::(\d+))?\b'

    def replacer(match):
        filepath = match.group(1)
        line_num = match.group(3)

        # Check if file exists (relative to working dir)
        full_path = Path(working_dir) / filepath
        if full_path.exists():
            # Convert Windows path to file:// URL format
            # Windows: file:///C:/path/to/file.py
            # Unix: file:///path/to/file.py
            file_url = full_path.as_posix()
            if file_url[1] == ':':  # Windows absolute path like C:/...
                file_url = f"/{file_url}"

            # OSC 8 hyperlink format: \033]8;;URL\033\\TEXT\033]8;;\033\\
            # This makes TEXT clickable but doesn't show any link markup
            display_text = f"{filepath}:{line_num}" if line_num else filepath
            url = f"file://{file_url}:{line_num}" if line_num else f"file://{file_url}"

            return f"\033]8;;{url}\033\\{display_text}\033]8;;\033\\"

        return match.group(0)  # Return unchanged if file doesn't exist

    return re.sub(pattern, replacer, text)


def main():
    """
    Entry point for the CodeCom CLI.

    Parses arguments, loads config, tests server connection, and enters the REPL loop.
    """
    parser = argparse.ArgumentParser(description="CodeCom V2 - CLI coding assistant")
    parser.add_argument("--config", "-c", help="Path to config.yaml", default=None)
    parser.add_argument("--dir", "-d", help="Working directory (overrides config)", default=None)
    parser.add_argument("--no-approval", action="store_true", help="Skip approval for destructive actions (dangerous)")
    args = parser.parse_args()

    # Load configuration from YAML file or environment variables
    config = load_config(args.config)

    # Determine working directory with priority: CLI arg > config file > current directory
    if args.dir:
        working_dir = os.path.abspath(args.dir)
    elif config.get("working_directory"):
        working_dir = os.path.abspath(config["working_directory"])
    else:
        working_dir = os.getcwd()

    os.chdir(working_dir)

    # Print the startup banner with connection info
    console.print(Panel(
        "[bold cyan]CodeCom V2[/bold cyan] - Your CLI Coding Assistant\n"
        f"[dim]Model: {config['model_name']}[/dim]\n"
        f"[dim]Endpoint: {config['api_base_url']}[/dim]\n"
        f"[dim]Working dir: {working_dir}[/dim]",
        border_style="cyan",
    ))

    # Initialize the LLM client
    client = LLMClient(config)

    # Test connection to the vLLM server
    console.print("\n[dim]Testing connection to vLLM server...[/dim]")
    if client.test_connection():
        console.print("[green]✓ Connected successfully![/green]\n")
    else:
        console.print("[red]✗ Could not connect to the vLLM server.[/red]")
        console.print(f"[red]  Check that your server is running at: {config['api_base_url']}[/red]")
        console.print("[dim]  Continuing anyway - you can fix the connection and try again.[/dim]\n")

    console.print("[dim]Commands: /quit (/q), /clear (/c), /dir (/d) <path>, /help (/h)[/dim]\n")

    # Conversation history (list of message dicts maintained for the session)
    messages = []
    skip_approval = args.no_approval
    use_streaming = config.get("streaming", True)

    try:
        session_loop(client, messages, working_dir, skip_approval, use_streaming)
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")
        sys.exit(0)


def session_loop(client: LLMClient, messages: list, working_dir: str, skip_approval: bool, use_streaming: bool):
    """
    Main interactive loop. Reads user input, handles commands, and triggers responses.

    Continues until the user types /quit, /exit, or presses Ctrl+C.
    """
    while True:
        try:
            # prompt_toolkit provides readline-style editing, history, and auto-suggest
            user_input = prompt(
                "You > ",
                history=FileHistory(str(HISTORY_FILE)),
                auto_suggest=AutoSuggestFromHistory(),
            ).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        # Handle slash commands (/quit, /clear, /dir, /help)
        if user_input.startswith("/"):
            if handle_command(user_input, messages, working_dir):
                continue
            else:
                break  # /quit returns False

        # Add user message to conversation and get a response
        messages.append({"role": "user", "content": user_input})
        handle_response(client, messages, working_dir, skip_approval, use_streaming)


def handle_command(command: str, messages: list, working_dir: str) -> bool:
    """
    Handle slash commands.

    Returns:
        True to continue the session loop, False to exit.
    """
    parts = command.split(maxsplit=1)
    cmd = parts[0].lower()

    if cmd in ("/quit", "/exit", "/q"):
        console.print("[dim]Goodbye![/dim]")
        return False
    elif cmd in ("/clear", "/c"):
        messages.clear()
        console.print("[green]Conversation cleared.[/green]\n")
    elif cmd in ("/dir", "/d"):
        if len(parts) > 1:
            new_dir = os.path.abspath(parts[1])
            if os.path.isdir(new_dir):
                os.chdir(new_dir)
                console.print(f"[green]Changed working directory to: {new_dir}[/green]\n")
            else:
                console.print(f"[red]Not a directory: {parts[1]}[/red]\n")
        else:
            console.print(f"[cyan]Current directory: {os.getcwd()}[/cyan]\n")
    elif cmd in ("/help", "/h"):
        console.print(Panel(
            "/quit, /q       - Exit CodeCom\n"
            "/clear, /c      - Clear conversation history\n"
            "/dir, /d [path] - Show or change working directory\n"
            "/help, /h       - Show this help",
            title="Commands",
            border_style="blue",
        ))
    else:
        console.print(f"[yellow]Unknown command: {cmd}[/yellow]\n")

    return True


def _handle_streaming_response(client: LLMClient, messages: list, working_dir: str = None) -> dict:
    """Handle a streaming response from the model, displaying it in a live-updating panel."""
    response_stream = client.chat(messages, stream=True)

    # Accumulate text as it streams
    accumulated_text = ""
    response = None

    # Create a live display with a panel
    console.print()
    with Live(
        Panel(
            Markdown(""),
            title="[bold cyan]CodeCom[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        ),
        console=console,
        refresh_per_second=10,
    ) as live:
        for chunk in response_stream:
            # Handle errors
            if "error" in chunk:
                live.stop()
                console.print(f"[red]API Error: {chunk['error']}[/red]\n")
                return None

            # Handle streaming deltas (incremental text)
            if "delta" in chunk:
                accumulated_text += chunk["delta"]
                # Clean tool call tags and make file paths clickable
                display_text = clean_tool_call_tags(accumulated_text)
                if working_dir:
                    display_text = make_paths_clickable(display_text, working_dir)

                # Update the live display with accumulated text rendered as Markdown
                live.update(
                    Panel(
                        Markdown(display_text),
                        title="[bold cyan]CodeCom[/bold cyan]",
                        border_style="cyan",
                        padding=(1, 2),
                    )
                )

            # Handle final result
            if chunk.get("done"):
                response = chunk
                break

    return response


def _handle_non_streaming_response(client: LLMClient, messages: list, working_dir: str = None) -> dict:
    """Handle a non-streaming response from the model with a spinner."""
    with console.status("[bold cyan]Thinking...[/bold cyan]", spinner="dots"):
        response = client.chat(messages, stream=False)

    # Handle API errors
    if "error" in response:
        console.print(f"[red]API Error: {response['error']}[/red]\n")
        return None

    # Display the model's text response (if any)
    if response["content"]:
        # Clean tool call tags and make file paths clickable
        display_text = clean_tool_call_tags(response["content"])
        if working_dir:
            display_text = make_paths_clickable(display_text, working_dir)

        console.print(Panel(
            Markdown(display_text),
            title="[bold cyan]CodeCom[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        ))

    return response


def handle_response(client: LLMClient, messages: list, working_dir: str, skip_approval: bool, use_streaming: bool = True):
    """
    The agentic response loop with streaming support.

    Sends the conversation to the model, streams the response as it's generated,
    parses any tool calls, executes them (with approval for destructive ones),
    feeds results back, and repeats until the model gives a final text answer.

    Safety: limited to 15 rounds to prevent infinite loops.
    """
    max_tool_rounds = 15  # Safety limit against runaway tool-calling loops

    for _ in range(max_tool_rounds):
        console.print()

        # Get response (streaming or non-streaming)
        if use_streaming:
            response = _handle_streaming_response(client, messages, working_dir)
        else:
            response = _handle_non_streaming_response(client, messages, working_dir)

        # If no response was received (error case)
        if response is None:
            messages.pop()  # Remove the user message that caused the error
            return

        # If no tool calls, this is the final answer — stop the loop
        if not response["tool_calls"]:
            messages.append({"role": "assistant", "content": response.get("raw_content", response["content"])})
            return

        # Store the model's full response (including tool call markup) in conversation history
        messages.append({"role": "assistant", "content": response.get("raw_content", response["content"])})

        # Execute each tool call from the model's response
        for tc in response["tool_calls"]:
            tool_name = tc["name"]

            # Parse the JSON arguments string into a dict
            try:
                tool_args = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                tool_result = f"Error: Could not parse tool arguments: {tc['arguments']}"
                messages.append({"role": "user", "content": f"[Tool Result for {tool_name}]: {tool_result}"})
                console.print(f"[red]{tool_result}[/red]")
                continue

            # Color-code tool calls based on type
            if tool_name in DESTRUCTIVE_TOOLS:
                tool_color = "yellow"
            elif tool_name.startswith("git_"):
                tool_color = "blue"
            else:
                tool_color = "green"

            # Show what tool is being called with color coding
            console.print(f"[dim]→ Tool: [{tool_color}]{tool_name}[/{tool_color}]({json.dumps(tool_args, indent=None)})[/dim]")

            # Check approval for destructive tools
            if not skip_approval:
                approved = request_approval(tool_name, tool_args)
                if not approved:
                    tool_result = "Action denied by user."
                    messages.append({"role": "user", "content": f"[Tool Result for {tool_name}]: {tool_result}"})
                    console.print("[yellow]  ✗ Denied[/yellow]")
                    continue

            # Execute the tool and get the result
            tool_result = execute_tool(tool_name, tool_args, working_dir)

            # Feed the result back to the model as a user message
            # (since we're using text-based tool calling, not native function calling)
            messages.append({"role": "user", "content": f"[Tool Result for {tool_name}]:\n{tool_result}"})

            # Display result preview to the user
            if tool_result.startswith("Error:"):
                console.print(f"[red]  {tool_result}[/red]")
            else:
                preview = tool_result[:200]
                if len(tool_result) > 200:
                    preview += "..."
                console.print(f"[green]  ✓ {preview}[/green]")

    # Safety: if we hit the max rounds, stop to prevent infinite loops
    console.print("[yellow]Reached maximum tool call rounds. Stopping.[/yellow]\n")


if __name__ == "__main__":
    main()
