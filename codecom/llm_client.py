"""
LLM Client for CodeCom V2.

Connects to a vLLM server via its OpenAI-compatible API (POST /v1/chat/completions).
Instead of using native function calling (which requires --enable-auto-tool-choice on the
server), this client uses TEXT-BASED tool calling:

1. The system prompt teaches the model to output tool calls in <tool_call> XML format
2. The model's text response is parsed with regex to extract tool calls
3. Tool results are fed back as user messages for the model to continue reasoning

This approach works with ANY vLLM deployment regardless of server-side tool calling config.

Tool Call Format (what the model outputs):
    <tool_call>
    <function=tool_name>
    <parameter=param_name>value</parameter>
    </function>
    </tool_call>
"""

import json
import re
from openai import OpenAI


# Regex to match the entire <tool_call>...</tool_call> block
# Captures: group(1) = function name, group(2) = parameters text
TOOL_CALL_PATTERN = re.compile(
    r'<tool_call>\s*<function=(\w+)>(.*?)(?:</function>)?\s*</tool_call>',
    re.DOTALL
)

# Regex to extract individual <parameter=name>value</parameter> pairs
# Captures: group(1) = parameter name, group(2) = parameter value
PARAM_PATTERN = re.compile(
    r'<parameter=(\w+)>(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|(?=</tool_call>))',
    re.DOTALL
)


class LLMClient:
    """
    Client that communicates with a vLLM server and parses tool calls from model output.

    The client sends plain chat messages (no native function calling) and parses
    any <tool_call> blocks from the model's text response.
    """

    def __init__(self, config: dict):
        """
        Initialize the LLM client.

        Args:
            config: Dictionary with keys:
                - api_base_url: vLLM server URL (e.g., "http://1.2.3.4:8080/v1")
                - api_key: Authentication key for the vLLM server
                - model_name: Model identifier (matches --served-model-name)
                - max_tokens: Maximum tokens in model response
                - temperature: Generation temperature (0 = deterministic)
                - system_prompt: Instructions for the model
        """
        # Use the OpenAI SDK but point it at our vLLM server instead of OpenAI
        self.client = OpenAI(
            base_url=config["api_base_url"],
            api_key=config["api_key"],
        )
        self.model = config["model_name"]
        self.max_tokens = config.get("max_tokens", 4096)
        self.temperature = config.get("temperature", 0.1)
        self.system_prompt = config.get("system_prompt", "You are a helpful coding assistant.")

    def chat(self, messages: list, use_tools: bool = True) -> dict:
        """
        Send a conversation to the model and parse the response.

        Args:
            messages: List of message dicts [{"role": "user/assistant", "content": "..."}]
            use_tools: Whether to include tool instructions (always True for now)

        Returns:
            dict with keys:
                - content: The model's text response (with tool_call blocks stripped out)
                - tool_calls: List of parsed tool calls [{id, name, arguments}]
                - finish_reason: Why the model stopped (stop, length, etc.)
                - raw_content: Original unmodified model output
                - error: (only present if request failed) Error message string
        """
        # Prepend the system prompt to the conversation
        full_messages = [{"role": "system", "content": self.system_prompt}] + messages

        try:
            # Send to vLLM (no tools= parameter — we use text-based tool calling)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        except Exception as e:
            return {"error": str(e)}

        choice = response.choices[0]
        raw_content = choice.message.content or ""

        # Parse any <tool_call> blocks from the model's text output
        tool_calls = self._parse_tool_calls(raw_content)

        # Remove tool_call blocks from the display content (user sees clean text)
        clean_content = TOOL_CALL_PATTERN.sub("", raw_content).strip()

        result = {
            "role": "assistant",
            "content": clean_content,
            "tool_calls": tool_calls,
            "finish_reason": choice.finish_reason,
            "raw_content": raw_content,
        }

        return result

    def _parse_tool_calls(self, text: str) -> list:
        """
        Extract tool calls from model text output.

        Parses blocks like:
            <tool_call>
            <function=read_file>
            <parameter=path>src/main.py</parameter>
            </function>
            </tool_call>

        Args:
            text: Raw model output text

        Returns:
            List of dicts: [{"id": "call_0", "name": "read_file", "arguments": '{"path": "src/main.py"}'}]
        """
        tool_calls = []
        matches = TOOL_CALL_PATTERN.finditer(text)

        for i, match in enumerate(matches):
            func_name = match.group(1)  # e.g., "read_file"
            params_text = match.group(2)  # everything between <function=X> and </tool_call>

            # Parse individual parameters from the params text
            args = {}
            param_matches = PARAM_PATTERN.finditer(params_text)
            for pm in param_matches:
                param_name = pm.group(1).strip()
                param_value = pm.group(2).strip()
                args[param_name] = param_value

            tool_calls.append({
                "id": f"call_{i}",
                "name": func_name,
                "arguments": json.dumps(args),  # Store as JSON string for consistency
            })

        return tool_calls

    def test_connection(self) -> bool:
        """
        Quick health check: send a trivial message and see if the server responds.

        Returns:
            True if the server is reachable and the model responds, False otherwise.
        """
        try:
            self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "Say hello in one word."}],
                max_tokens=10,
            )
            return True
        except Exception:
            return False
