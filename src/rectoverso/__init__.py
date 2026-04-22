"""rectoverso CLI — inspection, dry-run, preflight, and pipeline driver.

Run via `python -m rectoverso <command>` or (after install) `rectoverso <command>`.
See docs/cli.md for the command reference. Inspection subcommands are read-only;
the `run` subcommand drives Screenwriter + PromptSmith through the Anthropic
Messages API (or a deterministic stub under `--dry-run`) and writes the
manifest + event log.
"""

__version__ = "0.2.0"
