import json
import time

import pytest

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.slack import SlackAdapter
from gateway.restart_attribution import (
    ATTRIBUTION_FILENAME,
    MAX_AGE_SECONDS,
    MAX_ETA_CHARS,
    MAX_REASON_CHARS,
    consume_restart_attribution,
    format_attributed_message,
    parse_restart_attribution,
)
from tests.gateway.restart_test_helpers import make_restart_runner

BASE = "Singularity is going offline for maintenance."


def _payload(**overrides):
    data = {
        "actor": "jane.doe@comfrt.com",
        "reason": "Applying the new jira plugin config.",
        "eta": "About 5 minutes",
        "createdAtMs": time.time() * 1000,
    }
    data.update(overrides)
    return data


def _write(home, data):
    (home / ATTRIBUTION_FILENAME).write_text(json.dumps(data), encoding="utf-8")


def test_full_attribution():
    msg = format_attributed_message(BASE, parse_restart_attribution(_payload()))
    assert msg == (
        "Singularity is going offline for maintenance.\n"
        "Initiated by jane.doe@comfrt.com.\n"
        "Stated reasoning: Applying the new jira plugin config.\n"
        "User estimated time to return online: About 5 minutes"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": None, "eta": None},
        {"reason": "   "},
        {"eta": "\n\t"},
        {"actor": ""},
        {"actor": None},
        {"reason": 42},
        {"createdAtMs": None},
        {"createdAtMs": (time.time() - MAX_AGE_SECONDS - 5) * 1000},
    ],
)
def test_missing_blank_or_stale_falls_back_to_plain_message(overrides):
    data = {k: v for k, v in _payload(**overrides).items() if v is not None}
    attribution = parse_restart_attribution(data)
    assert attribution is None
    assert format_attributed_message(BASE, attribution) == BASE


def test_empty_args_falls_back():
    assert parse_restart_attribution({}) is None
    assert parse_restart_attribution("nope") is None


def test_multiline_reason_is_quoted_and_trimmed():
    attribution = parse_restart_attribution(
        _payload(reason="  Line one\r\nLine two   \n\nLine four\n  ")
    )
    msg = format_attributed_message(BASE, attribution)
    assert msg == (
        "Singularity is going offline for maintenance.\n"
        "Initiated by jane.doe@comfrt.com.\n"
        "Stated reasoning:\n"
        "> Line one\n"
        "> Line two\n"
        "> \n"
        "> Line four\n"
        "User estimated time to return online: About 5 minutes"
    )


def test_mentions_and_links_are_escaped():
    attribution = parse_restart_attribution(
        _payload(
            reason="ping <!channel> and <@U123> & <https://evil.example|click>",
            eta="soon [here](https://evil.example) <!here>",
        )
    )
    msg = format_attributed_message(BASE, attribution)
    assert "Stated reasoning: ping &lt;!channel&gt; and &lt;@U123&gt; &amp; &lt;https://evil.example|click&gt;" in msg
    assert "<!" not in msg and "<@" not in msg and "<http" not in msg
    assert "[here]\u200b(https://evil.example)" in msg


def test_escaped_text_survives_slack_formatting():
    attribution = parse_restart_attribution(
        _payload(reason="<!channel> <@U123> [x](https://evil.example)\n> fake quote", eta="> 5 min & <!here>")
    )
    adapter = SlackAdapter.__new__(SlackAdapter)
    out = SlackAdapter.format_message(adapter, format_attributed_message(BASE, attribution))
    assert "<!channel>" not in out and "<@U123>" not in out and "<!here>" not in out
    assert "<https://evil.example" not in out
    assert "&lt;!channel&gt;" in out
    assert "> &gt; fake quote" in out
    assert "User estimated time to return online: &gt; 5 min &amp; &lt;!here&gt;" in out


def test_lengths_are_capped():
    attribution = parse_restart_attribution(
        _payload(reason="r" * (MAX_REASON_CHARS + 500), eta="e" * (MAX_ETA_CHARS + 500))
    )
    assert len(attribution["reason"]) == MAX_REASON_CHARS + 1
    assert attribution["reason"].endswith("…")
    assert len(attribution["eta"]) == MAX_ETA_CHARS + 1


def test_consume_reads_once_and_deletes(tmp_path):
    _write(tmp_path, _payload())
    assert consume_restart_attribution(tmp_path)["actor"] == "jane.doe@comfrt.com"
    assert not (tmp_path / ATTRIBUTION_FILENAME).exists()
    assert consume_restart_attribution(tmp_path) is None


def test_consume_deletes_invalid_file(tmp_path):
    (tmp_path / ATTRIBUTION_FILENAME).write_text("{not json", encoding="utf-8")
    assert consume_restart_attribution(tmp_path) is None
    assert not (tmp_path / ATTRIBUTION_FILENAME).exists()


def _slack_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    runner.config.platforms = {
        Platform.SLACK: PlatformConfig(
            enabled=True,
            token="***",
            home_channel=HomeChannel(platform=Platform.SLACK, chat_id="C_HOME", name="status"),
            gateway_restart_messages={"shutting_down": BASE, "restarting": "Singularity is restarting."},
        )
    }
    runner.adapters = {Platform.SLACK: adapter}
    return runner, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restart_requested", "base"),
    [(False, BASE), (True, "Singularity is restarting.")],
)
async def test_shutdown_notification_includes_attribution(tmp_path, monkeypatch, restart_requested, base):
    runner, adapter = _slack_runner(tmp_path, monkeypatch)
    runner._restart_requested = restart_requested
    _write(tmp_path, _payload())

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent_calls[0][0] == "C_HOME"
    assert adapter.sent_calls[0][1] == (
        f"{base}\n"
        "Initiated by jane.doe@comfrt.com.\n"
        "Stated reasoning: Applying the new jira plugin config.\n"
        "User estimated time to return online: About 5 minutes"
    )
    assert not (tmp_path / ATTRIBUTION_FILENAME).exists()


@pytest.mark.asyncio
async def test_shutdown_notification_without_attribution_is_plain(tmp_path, monkeypatch):
    runner, adapter = _slack_runner(tmp_path, monkeypatch)

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent_calls[0][1] == BASE
