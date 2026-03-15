"""
Starter renderer — JSON config → HTML → PDF

Install deps:
    pip install jinja2 weasyprint

Usage:
    python starter_renderer.py example_config.json output/handout.pdf
"""

import json
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def load_config(config_path: str) -> dict:
    """Load and return the agent's JSON config."""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def render_html(config: dict, template_dir: str = "templates") -> str:
    """Render a Jinja2 template using the config data."""
    env = Environment(loader=FileSystemLoader(template_dir))
    condition = config.get("condition", "HTN")
    template = env.get_template(f"{condition}_handout.html")
    return template.render(**config)


def save_html(html_content: str, output_path: str) -> None:
    """Save rendered HTML to file (for preview/debugging)."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"HTML saved: {output_path}")


def render_pdf(html_content: str, output_path: str) -> None:
    """Convert rendered HTML to PDF via WeasyPrint."""
    try:
        from weasyprint import HTML
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        HTML(string=html_content).write_pdf(output_path)
        print(f"PDF saved: {output_path}")
    except ImportError:
        print("WeasyPrint not installed. Run: pip install weasyprint")
        print("Falling back to HTML-only output.")
        html_path = output_path.replace(".pdf", ".html")
        save_html(html_content, html_path)


def main():
    if len(sys.argv) < 2:
        print("Usage: python starter_renderer.py <config.json> [output.pdf]")
        sys.exit(1)

    config_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "output/handout.pdf"

    config = load_config(config_path)
    print(f"Loaded config for: {config['patient']['name']} — {config['condition']}")

    html = render_html(config)

    if output_path.endswith(".pdf"):
        render_pdf(html, output_path)
    else:
        save_html(html, output_path)


if __name__ == "__main__":
    main()
