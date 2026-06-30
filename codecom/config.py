"""
Configuration Loader for CodeCom V2.

Loads runtime settings from (in order of priority):
1. A YAML config file (default: config.yaml in working directory)
2. Environment variables (VLLM_API_BASE, VLLM_MODEL, VLLM_API_KEY)
3. Built-in defaults

The config file path can be overridden:
- Via CLI: codecom --config /path/to/config.yaml
- Via env var: CODECOM_CONFIG=/path/to/config.yaml
"""

import os
import yaml
from pathlib import Path


def load_config(config_path: str = None) -> dict:
    """
    Load configuration from YAML file with environment variable fallbacks.

    Args:
        config_path: Optional explicit path to config.yaml.
                     If None, checks CODECOM_CONFIG env var, then falls back to ./config.yaml.

    Returns:
        dict with keys: api_base_url, model_name, api_key, max_tokens, temperature, system_prompt
    """
    # Determine which config file to load
    if config_path is None:
        config_path = os.environ.get("CODECOM_CONFIG", "config.yaml")

    path = Path(config_path)

    # If no config file exists, fall back entirely to environment variables / defaults
    if not path.exists():
        return {
            "api_base_url": os.environ.get("VLLM_API_BASE", "http://localhost:8000/v1"),
            "model_name": os.environ.get("VLLM_MODEL", "default"),
            "api_key": os.environ.get("VLLM_API_KEY", "token-abc123"),
            "max_tokens": 4096,
            "temperature": 0.1,
            "system_prompt": "You are a helpful coding assistant with file system tools.",
        }

    # Load the YAML config file
    with open(path, "r") as f:
        config = yaml.safe_load(f)

    # Apply defaults for any missing keys (makes config file partially optional)
    config.setdefault("api_base_url", "http://localhost:8000/v1")
    config.setdefault("model_name", "default")
    config.setdefault("api_key", "token-abc123")
    config.setdefault("max_tokens", 4096)
    config.setdefault("temperature", 0.1)

    return config
