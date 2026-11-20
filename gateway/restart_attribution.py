"""Operator attribution for gateway restarts triggered from Atls.

The singularity-commander (a separate process) receives a ``restart-gateway``
command carrying ``actor``, ``args.reason`` and ``args.eta``, writes them to
``<HERMES_HOME>/.restart_attribution.json`` and then runs
``hermes gateway restart``. The gateway consumes (reads and deletes) that file
while composing its shutdown notification so the Slack "going offline" post
says who restarted it, why, and when it should be back.

``reason`` and ``eta`` are untrusted user text: they are length-capped and
Slack-escaped here so they can't inject mentions (``<!channel>``, ``<@U…>``)
or links.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)

ATTRIBUTION_FILENAME = ".restart_attribution.json"

MAX_ACTOR_CHARS = 320
MAX_REASON_CHARS = 2000
MAX_ETA_CHARS = 500

# The file is written moments before the restart signal; anything older is a
# leftover from a restart that never happened and must not be reused.
MAX_AGE_SECONDS = 10 * 60
_MAX_CLOCK_SKEW_SECONDS = 60


class RestartAttribution(TypedDict):
    actor: str
    reason: str
    eta: str


def attribution_path(hermes_home: Path) -> Path:
    return Path(hermes_home) / ATTRIBUTION_FILENAME


def _clean(value: Any, cap: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > cap:
        text = text[:cap].rstrip() + "…"
    return text


def parse_restart_attribution(data: Any, *, now: Optional[float] = None) -> Optional[RestartAttribution]:
    """Validate a decoded attribution payload; None unless fully attributed and fresh."""
    if not isinstance(data, dict):
        return None
    created_ms = data.get("createdAtMs")
    if not isinstance(created_ms, (int, float)) or isinstance(created_ms, bool):
        return None
    age = (time.time() if now is None else now) - created_ms / 1000.0
    if age > MAX_AGE_SECONDS or age < -_MAX_CLOCK_SKEW_SECONDS:
        return None
    actor = " ".join(_clean(data.get("actor"), MAX_ACTOR_CHARS).split())
    reason = _clean(data.get("reason"), MAX_REASON_CHARS)
    eta = _clean(data.get("eta"), MAX_ETA_CHARS)
    if not (actor and reason and eta):
        return None
    return {"actor": actor, "reason": reason, "eta": eta}


def consume_restart_attribution(hermes_home: Path) -> Optional[RestartAttribution]:
    """Read and delete the attribution file. Never raises."""
    path = attribution_path(hermes_home)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug("Failed reading restart attribution %s: %s", path, e)
        raw = None
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Failed removing restart attribution %s: %s", path, e)
    if raw is None:
        return None
    try:
        return parse_restart_attribution(json.loads(raw))
    except Exception as e:
        logger.debug("Ignoring malformed restart attribution %s: %s", path, e)
        return None


def escape_slack_text(text: str) -> str:
    """Escape Slack mrkdwn control characters and defuse markdown links.

    The Slack adapter's ``format_message`` turns ``[label](url)`` into a real
    link, so a zero-width space is inserted between ``]`` and ``(``.
    """
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("](", "]\u200b(")


def _labelled(label: str, value: str) -> str:
    lines = [line.rstrip() for line in escape_slack_text(value).split("\n")]
    if len(lines) == 1:
        return f"{label} {lines[0]}"
    return "\n".join([label] + [f"> {line}" for line in lines])


def format_attributed_message(base: str, attribution: Optional[RestartAttribution]) -> str:
    """Append attribution lines to a lifecycle message, or return it unchanged."""
    if not attribution:
        return base
    return "\n".join(
        [
            base,
            f"Initiated by {escape_slack_text(attribution['actor'])}.",
            _labelled("Stated reasoning:", attribution["reason"]),
            _labelled("User estimated time to return online:", attribution["eta"]),
        ]
    )
