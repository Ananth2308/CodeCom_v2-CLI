"""
CodeCom V2 - CLI Coding Assistant

A terminal-based interactive coding assistant that connects to your own
LLM hosted via vLLM. Provides file system tools (read, edit, append, write,
list, search) with human-in-the-loop approval for destructive operations.

Modules:
    cli         - Main interactive REPL loop and terminal UI
    config      - Configuration loading from YAML and environment variables
    llm_client  - OpenAI-compatible API client with text-based tool call parsing
    tools       - File system tool definitions and execution engine
    approval    - Human-in-the-loop approval gates for destructive operations
"""

__version__ = "0.1.0"
