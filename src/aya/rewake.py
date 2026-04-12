"""Shared asyncRewake emitter for Claude Code hooks.

Claude Code's asyncRewake hooks write a JSON payload to stdout when a
background event completes.  Claude reads the payload on the next idle
tick and injects ``additionalContext`` into the agent turn.

Any hook that needs to wake Claude mid-session should call ``emit()``
rather than formatting the JSON by hand.
"""

from __future__ import annotations

import json
import sys


def emit(context: str, event_name: str = "PostToolUse") -> None:
    """Write an asyncRewake JSON payload to stdout.

    Args:
        context: Human-readable message injected into Claude's next turn.
        event_name: Hook event name (default ``PostToolUse``).
    """
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": context,
                }
            }
        )
        + "\n"
    )
