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
import argparse
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
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


def main():
    """
    Entry point for the CodeCom CLI.

    Parses arguments, loads config, tests server connection, and enters the REPL loop.
    """
    parser = argparse.ArgumentParser(description="CodeCom V2 - CLI coding assistant")
    parser.add_argument("--config", "-c", help="Path to config.yaml", default=None)
    parser.add_argument("--dir", "-d", help="Working directory", default=os.getcwd())
    parser.add_argument("--no-approval", action="store_true", help="Skip approval for destructive actions (dangerous)")
    args = parser.parse_args()

    # Set the working directory (all file operations are relative to this)
    working_dir = os.path.abspath(args.dir)
    os.chdir(working_dir)

    # Load configuration from YAML file or environment variables
    config = load_config(args.config)

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

    console.print("[dim]Commands: /quit, /clear, /dir <path>, /help[/dim]\n")

    # Conversation history (list of message dicts maintained for the session)
    messages = []
    skip_approval = args.no_approval

    try:
        session_loop(client, messages, working_dir, skip_approval)
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")
        sys.exit(0)


def session_loop(client: LLMClient, messages: list, working_dir: str, skip_approval: bool):
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
        handle_response(client, messages, working_dir, skip_approval)


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
    elif cmd == "/clear":
        messages.clear()
        console.print("[green]Conversation cleared.[/green]\n")
    elif cmd == "/dir":
        if len(parts) > 1:
            new_dir = os.path.abspath(parts[1])
            if os.path.isdir(new_dir):
                os.chdir(new_dir)
                console.print(f"[green]Changed working directory to: {new_dir}[/green]\n")
            else:
                console.print(f"[red]Not a directory: {parts[1]}[/red]\n")
        else:
            console.print(f"[cyan]Current directory: {os.getcwd()}[/cyan]\n")
    elif cmd == "/help":
        console.print(Panel(
            "/quit      - Exit CodeCom\n"
            "/clear     - Clear conversation history\n"
            "/dir [path] - Show or change working directory\n"
            "/help      - Show this help",
            title="Commands",
            border_style="blue",
        ))
    else:
        console.print(f"[yellow]Unknown command: {cmd}[/yellow]\n")

    return True


def handle_response(client: LLMClient, messages: list, working_dir: str, skip_approval: bool):
    """
    The agentic response loop.

    Sends the conversation to the model, parses any tool calls from the response,
    executes them (with approval for destructive ones), feeds results back, and
    repeats until the model gives a final text answer.

    Safety: limited to 15 rounds to prevent infinite loops.
    """
    max_tool_rounds = 15  # Safety limit against runaway tool-calling loops

    for _ in range(max_tool_rounds):
        console.print()
        # Show a spinner while waiting for the model's response
        with console.status("[bold cyan]Thinking...[/bold cyan]", spinner="dots"):
            response = client.chat(messages)

        # Handle API errors
        if "error" in response:
            console.print(f"[red]API Error: {response['error']}[/red]\n")
            messages.pop()  # Remove the user message that caused the error
            return

        # Display the model's text response (if any)
        if response["content"]:
            console.print(Panel(
                Markdown(response["content"]),
                title="[bold cyan]CodeCom[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            ))

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

            # Show what tool is being called
            console.print(f"[dim]→ Tool: {tool_name}({json.dumps(tool_args, indent=None)})[/dim]")

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
