# CodeCom V2

A CLI coding assistant (like Claude Code) that connects to **your own LLM** hosted via vLLM on AWS EC2. It provides an interactive terminal interface where you can ask your model to understand, read, edit, and append to your codebase — with human-in-the-loop approval before any destructive file operation.

## Features

- Connects to any vLLM-hosted model via the OpenAI-compatible API
- Interactive terminal REPL with rich formatting
- File system tools: read, edit, append, write, list directory tree, search
- Human-in-the-loop: asks for approval before any file modification
- Conversation history within a session
- Configurable via YAML or environment variables

## Prerequisites

- Python 3.9+
- A running vLLM server (on your EC2 instance or locally)

## Setup

### 1. Install dependencies

```bash
cd CodeCom_V2
pip install -r requirements.txt

 .
```

Or without installing as a package:

```bash
pip install -r requirements.txt
```

### 2. Start your vLLM server (on your EC2 instance)

```bash
# Example: serving a model with vLLM
unset VLLM_ATTENTION_BACKEND && python -m vllm.entrypoints.openai.api_server       --model <your model name>       --served-model-name <your model name>        --host 0.0.0.0       --port <Any port>       --dtype auto       --tensor-parallel-size 4       --pipeline-parallel-size 1       --gpu-memory-utilization 0.90       --max-num-seqs 16       --enforce-eager       --enable-auto-tool-choice       --tool-call-parser hermes       --api-key <your_api_key>
```

Make sure:
- Port 8000 (or your chosen port) is open in your EC2 security group
- You can reach the server from your local machine

### 3. Configure CodeCom

Edit `config.yaml`:

```yaml
# Your EC2 instance URL
api_base_url: "http://<your-ec2-public-ip>:8000/v1"

# Must match --served-model-name (or the HuggingFace model path)
model_name: "your model name"

# If you set --api-key in vLLM, put it here
api_key: "token-abc123"

# Optional: Set default working directory
working_directory: "/path/to/your/project"
```

Or use environment variables:

```bash
export VLLM_API_BASE="http://<your-ec2-public-ip>:8000/v1"
export VLLM_MODEL="your model name"
export VLLM_API_KEY="token-abc123"
export CODECOM_WORKING_DIR="/path/to/your/project"
```

### 4. Run CodeCom

```bash
# If installed with pip install -e .
codecom

# Or run directly
python run.py

# Specify a working directory
codecom --dir /path/to/your/project

# Skip approval gates (dangerous!)
codecom --no-approval
```

## Usage

Once running, you'll see an interactive prompt. Ask your model anything about your codebase:

```
You > What files are in this project?
You > Read the main.py file and explain what it does
You > Add error handling to the process_data function in utils.py
You > Create a new test file for the auth module
```

### Commands

| Command | Description |
|---------|-------------|
| `/quit` | Exit CodeCom |
| `/clear` | Clear conversation history |
| `/dir [path]` | Show or change working directory |
| `/help` | Show available commands |

### Human-in-the-Loop

When the model wants to modify a file (edit, append, or write), you'll see an approval prompt:

```
⚠ Approval Required: edit_file
┌─────────────────────────────────────┐
│ File: src/main.py                   │
│                                     │
│ - old_code_here                     │
│ + new_code_here                     │
└─────────────────────────────────────┘
[y]es / [n]o / [a]lways:
```

- `y` — approve this action
- `n` — deny this action
- `a` — approve and stop asking for this tool type

## Architecture

```
CodeCom_V2/
├── config.yaml          # Configuration (endpoint, model name, etc.)
├── run.py               # Entry point
├── pyproject.toml       # Package definition
├── requirements.txt     # Dependencies
└── codecom/
    ├── __init__.py
    ├── cli.py           # Main REPL loop and UI
    ├── config.py        # Config loading (YAML + env vars)
    ├── llm_client.py    # OpenAI-compatible API client for vLLM
    ├── tools.py         # File system tools (read, edit, append, etc.)
    └── approval.py      # Human-in-the-loop approval gates
```

## How It Works

1. Your message is sent to the vLLM model via the OpenAI-compatible chat completions API
2. The model can respond with text, or request to use a tool (function calling)
3. If a tool is destructive (file edit/write/append), you're asked for approval
4. Tool results are sent back to the model for further reasoning
5. This loop continues until the model responds with just text (no tool calls)

## Troubleshooting

**"Could not connect to the vLLM server"**
- Verify your EC2 instance is running
- Check the security group allows inbound traffic on port 8000
- Test with: `curl http://<your-ec2-ip>:8000/v1/models`

**Model not using tools**
- Not all models support function calling well. Models like Llama 3.1 70B+, Qwen2.5 72B, and Mistral Large work best.
- Smaller models (<13B parameters) may struggle with tool use.

**Slow responses**
- This depends on your EC2 instance GPU(s) and model size
- Consider using tensor parallelism (`--tensor-parallel-size`) for larger models
