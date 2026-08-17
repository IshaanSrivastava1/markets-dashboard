"""
agent_kit -- a tiny shared wrapper around the Anthropic API.

This exact file is copied into three projects:
  - job-alerts/            (Agent A: job fit scoring)
  - Markets Dashboard/     (Agent B: gold market brief)
  - ~/portfolio-agent/     (Agent C: portfolio page writer)

It is duplicated on purpose rather than published as a package -- three copies
of 150 lines is less overhead than maintaining a pip package. The tradeoff is
drift: if you fix something here, copy it to the other two.

Two entry points, one for each rung of the "agent" ladder:

  ask_json()  -- one request, one JSON answer. No loop, no tools.
                 This is what Agent A uses.

  run_agent() -- give Claude tools and let it decide what to call, in what
                 order, and when it is done. This is a real agent loop.
                 Agents B and C use this.

Everything raises LLMUnavailable on failure so callers can fail *open* --
a dead API key must never break job alerts or a page build.
"""

import functools
import json
import os
import sys

# The single cost knob for all three agents. Override per-repo via env.
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

# Print every tool call as it happens. The whole point of building an agent
# is watching the loop, so this is on by default.
VERBOSE = os.environ.get("AGENT_VERBOSE", "1") == "1"


class LLMUnavailable(Exception):
    """Raised for any API failure. Callers should catch this and degrade."""


_CLIENT = None


def _client():
    """Lazy singleton, so importing this module never requires a key."""
    global _CLIENT
    if _CLIENT is None:
        try:
            import anthropic
        except ImportError as e:
            raise LLMUnavailable(
                "the 'anthropic' package is not installed "
                "(pip install -r requirements.txt)") from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        _CLIENT = anthropic.Anthropic()
    return _CLIENT


def _log(msg):
    if VERBOSE:
        print("  [llm] %s" % msg, file=sys.stderr, flush=True)


def safe(default=None):
    """Decorator: swallow LLMUnavailable and return `default` instead.

    Use this on anything in a production path. A scoring failure should cost
    you the score, not the alert.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except LLMUnavailable as e:
                _log("degraded: %s" % e)
                return default
            except Exception as e:  # noqa: BLE001 - deliberate catch-all
                _log("degraded (unexpected %s): %s" % (type(e).__name__, e))
                return default
        return wrapper
    return deco


# --- Rung 1: a single call that returns structured JSON ---------------------

def ask_json(system, user, schema, max_tokens=2048, effort="low"):
    """One request in, one validated dict out.

    `schema` is a JSON Schema object. The API *enforces* it (structured
    outputs), so the result is guaranteed to parse and to have your keys --
    no defensive parsing, no retry-on-bad-JSON loop.

    `system` is sent as a cacheable block. When the same system text is reused
    across calls (e.g. a resume), repeat calls bill the cached portion at ~10%.
    Note: prompt caching has a 1024-token minimum on Sonnet 5 -- shorter system
    prompts silently just don't cache, which is harmless.
    """
    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user}],
            thinking={"type": "disabled"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
    except Exception as e:  # noqa: BLE001 - SDK raises many types
        raise LLMUnavailable("%s: %s" % (type(e).__name__, e)) from e

    if resp.stop_reason == "refusal":
        raise LLMUnavailable("model refused the request")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise LLMUnavailable("empty response (stop_reason=%s)" % resp.stop_reason)

    usage = resp.usage
    _log("ask_json ok (in=%s cached=%s out=%s)" % (
        usage.input_tokens, usage.cache_read_input_tokens, usage.output_tokens))
    return json.loads(text)


# --- Rung 3: a real agent loop ----------------------------------------------

def run_agent(system, user, tools, max_tokens=8192, max_iterations=12,
              max_restarts=3):
    """Give Claude tools and let it drive.

    `tools` may mix:
      - functions decorated with @anthropic.beta_tool (executed locally by the
        SDK's tool runner -- you write the function, it calls it)
      - raw server-tool dicts like {"type": "web_search_20260209",
        "name": "web_search"} (executed on Anthropic's side; nothing to
        implement)

    Returns the final assistant message. Every tool call is logged so you can
    watch the loop: model picks a tool -> code runs it -> result goes back ->
    repeat until the model stops asking.
    """
    client = _client()
    messages = [{"role": "user", "content": user}]
    last = None
    restarts = 0

    while True:
        try:
            runner = client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=max_tokens,
                max_iterations=max_iterations,
                system=[{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=tools,
                messages=messages,
            )
        except Exception as e:  # noqa: BLE001
            raise LLMUnavailable("%s: %s" % (type(e).__name__, e)) from e

        steps = 0
        try:
            for message in runner:
                last = message
                steps += 1

                for block in message.content:
                    if block.type == "text" and block.text.strip():
                        _log("says: %s" % block.text.strip()[:160])
                    elif block.type == "tool_use":
                        _log("calls %s(%s)" % (
                            block.name,
                            json.dumps(block.input)[:160]))
                    elif block.type == "server_tool_use":
                        _log("server tool %s(%s)" % (
                            block.name,
                            json.dumps(block.input)[:160]))

                # Mirror history ourselves so we can resume after a pause_turn;
                # the runner keeps its own copy and does not expose it.
                messages.append({"role": "assistant", "content": message.content})
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    messages.append(tool_response)

                if steps >= max_iterations:
                    _log("hit max_iterations=%d, stopping" % max_iterations)
                    return last
        except Exception as e:  # noqa: BLE001
            raise LLMUnavailable("%s: %s" % (type(e).__name__, e)) from e

        # Server-side tools (web search) can stop the turn with "pause_turn".
        # The Python runner does not auto-resume, so restart it with the
        # paused turn already appended to history.
        if last is None or getattr(last, "stop_reason", None) != "pause_turn":
            return last
        restarts += 1
        if restarts > max_restarts:
            _log("still paused after %d restarts, giving up" % max_restarts)
            return last
        _log("pause_turn -- resuming (restart %d)" % restarts)


def text_of(message):
    """Concatenate the text blocks of a message. '' if there are none."""
    if message is None:
        return ""
    return "\n".join(b.text for b in message.content if b.type == "text").strip()
