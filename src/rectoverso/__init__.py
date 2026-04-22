"""rectoverso CLI — inspection, dry-run, and preflight operations.

Run via `python -m rectoverso <command>` or (after install) `rectoverso <command>`.
See docs/cli.md for the command reference. Every subcommand is read-only or
dry-run; nothing here dispatches tools, calls APIs, or spends budget. The
orchestration loop is invoked separately once it lands.
"""

__version__ = "0.1.0"
