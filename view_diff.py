#!/usr/bin/env python3
import subprocess
import sys
from rich.console import Console

console = Console()

def get_git_diff(file_path=None):
    cmd = ["git", "diff", "HEAD"]
    if file_path:
        cmd.append(file_path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout

def display_diff(diff_text):
    if not diff_text.strip():
        console.print("[yellow]No changes[/yellow]")
        return

    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                file_name = parts[3].replace("b/", "")
                console.print(f"\n[bold cyan]{file_name}[/bold cyan]")
        elif line.startswith("@@"):
            console.print(f"[blue]{line}[/blue]", markup=False, highlight=False)
        elif line.startswith("+") and not line.startswith("+++"):
            console.print(f"[green]{line}[/green]", markup=False, highlight=False)
        elif line.startswith("-") and not line.startswith("---"):
            console.print(f"[red]{line}[/red]", markup=False, highlight=False)
        elif line.startswith(" "):
            console.print(line, highlight=False)

file_path = sys.argv[1] if len(sys.argv) > 1 else None
diff_text = get_git_diff(file_path)
display_diff(diff_text)
