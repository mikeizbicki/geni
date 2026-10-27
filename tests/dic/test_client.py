"""Integration tests: a real config file, a real sqlite file, a real socket.

Each test is one dic() call -- prompt in, answer out, one row written -- because
that is the unit a user has, and a failure should name the part of the call
that broke.  A pure function is doctested where it lives, not retested here.
"""
import hashlib, json, os

import pytest

from dic.client import dic
from dic.store import db, session_path
from dic.tty import DicError


def say(streams, env, prompt, **knobs):
    """One dic() call through the fake streams and the test environment."""
    return dic(prompt, env=env, out=streams.out, err=streams.err, **knobs)


def tree(env):
    """The message tree, oldest first."""
    conn = db(env)
    rows = conn.execute("SELECT * FROM messages ORDER BY t_start").fetchall()
    conn.close()
    return rows


def test_one_call_prints_the_answer_and_appends_one_row(streams, env, sse, model):
    reply = say(streams, env, "hi", model=model)

    assert streams.out.getvalue() == "hello\n"
    assert reply.text == "hello\n"
    assert (reply.status, reply.model_id) == (200, "fake")

    row = tree(env)[0]
    assert row["mid"] == reply.mid
    assert (row["user"], row["response"], row["status"]) == ("hi", "hello", 200)
    assert row["t_start"] <= row["t_request"] <= row["t_first"] <= row["t_last"]
    assert os.path.exists(session_path(env))     # something for -c to point at


def test_the_request_names_the_model_and_carries_the_prompt(streams, env, sse, model):
    say(streams, env, "hi", model=model)

    request = sse.received[-1]
    assert request.path == "/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.body["model"] == "fake-1"
    assert request.body["messages"] == [{"role": "user", "content": "hi"}]


def test_the_cost_line_reports_the_tokens_the_api_counted(streams, env, sse, model):
    streams.err.tty = True                       # a terminal gets the cost line
    reply = say(streams, env, "hi", model=model)

    assert ("cost: $0.0105 (input: $0.0030, output: $0.0075)"
            f" --mid={reply.mid}") in streams.err.getvalue()
    row = tree(env)[0]
    assert (row["tokens_input"], row["tokens_output"]) == (1000, 500)


def test_continue_replays_the_conversation(streams, env, sse, model, events):
    first = say(streams, env, "one", model=model)
    sse.events = events("second")
    say(streams, env, "two", model=model, cont=True)

    assert sse.received[-1].body["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "two"}]
    assert streams.out.getvalue() == "hello\nsecond\n"
    assert tree(env)[1]["prev_mid"] == first.mid


def test_mid_and_continue_together_are_an_error(streams, env, sse, model):
    # -c names this session's pointer and --mid names a ref; both at once is
    # two answers to one question, so it is refused rather than won.
    first = say(streams, env, "one", model=model)
    with pytest.raises(DicError, match="pick one"):
        say(streams, env, "two", model=model, cont=True, mid=first.mid)

    assert len(sse.received) == 1        # refused before the network


def test_a_failed_call_is_stored_and_leaves_the_pointer_alone(streams, env, sse, model):
    sse.status = 503
    with pytest.raises(DicError, match="503"):
        say(streams, env, "hi", model=model)

    row = tree(env)[0]
    assert row["status"] == 503 and "overloaded" in row["error"]
    assert row["response"] == ""                 # nothing to replay next turn
    assert not os.path.exists(session_path(env))


def test_continue_without_a_session_pointer_is_an_error(streams, env, model):
    with pytest.raises(DicError, match="no conversation"):
        say(streams, env, "hi", model=model, cont=True)


def test_extract_prints_the_code_block_but_stores_the_whole_answer(streams, env, sse,
                                                                   model, events):
    sse.events = events("prose\n```python\nx = 1\n```\ntail")
    reply = say(streams, env, "hi", model=model, extract=True)

    assert streams.out.getvalue() == "x = 1\n"
    assert reply.text == "x = 1\n"
    assert tree(env)[0]["response"] == "prose\n```python\nx = 1\n```\ntail"


def test_an_attachment_is_stored_verbatim_and_sent_as_a_data_url(streams, env, sse,
                                                                 model, tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    say(streams, env, "what is this", model=model, attachment=[str(image)])

    parts = sse.received[-1].body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1] == {"type": "text", "text": "what is this"}

    aid = json.loads(tree(env)[0]["attachments"])[0]
    conn = db(env)
    row = conn.execute("SELECT path, hash FROM attachments WHERE aid=?",
                       (aid,)).fetchone()
    conn.close()
    assert row["path"] == str(image)
    assert row["hash"] == hashlib.sha256(image.read_bytes()).hexdigest()


def test_a_turn_is_replayed_verbatim_to_the_protocol_that_produced_it(streams, env,
                                                                      sse, models,
                                                                      events):
    sse.events = events("first", "anthropic-messages")
    first = say(streams, env, "one", model="fake+anthropic")
    say(streams, env, "two", model="fake+anthropic", mid=first.mid)

    assert sse.received[-1].path == "/messages"
    assert sse.received[-1].body["messages"][1] == {
        "role": "assistant", "content": [{"type": "text", "text": "first"}]}


def test_a_turn_is_converted_for_a_call_that_speaks_another_protocol(streams, env,
                                                                     sse, model,
                                                                     events):
    sse.events = events("first", "anthropic-messages")
    first = say(streams, env, "one", model="fake+anthropic")
    sse.events = events("second")
    say(streams, env, "two", model=model, mid=first.mid)

    assert sse.received[-1].body["messages"][1] == {
        "role": "assistant", "content": "first"}
    assert tree(env)[1]["api_type"] == "openai-chat"


# A failure that arrives after the 200, and the provider's own reason for
# stopping, are the two things a stream can still say.  client.py turns the
# first into a -1 row that -c will not continue from and the second into a
# warning, so an adaptor recording neither is silently writing a 200 for a
# call that failed.  One event list per protocol, so that is a test failure
# and not a mystery.
STREAM_ERRORS = {
    "fake": [
        {"choices": [{"delta": {"content": "par"}}]},
        {"error": {"type": "server_error", "message": "overloaded"}}],
    "fake+anthropic": [
        {"type": "content_block_start",
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "par"}},
        {"type": "error",
         "error": {"type": "overloaded_error", "message": "overloaded"}}],
    "fake+responses": [
        {"type": "response.output_text.delta", "delta": "par"},
        {"type": "response.failed",
         "response": {"error": {"code": "server_error",
                                "message": "overloaded"}}}],
}

TRUNCATED = {
    "fake": ("length", [
        {"choices": [{"delta": {"content": "half"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]}]),
    "fake+anthropic": ("max_tokens", [
        {"type": "content_block_start",
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "half"}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}]),
    "fake+responses": ("max_output_tokens", [
        {"type": "response.output_text.delta", "delta": "half"},
        {"type": "response.incomplete",
         "response": {"output": [{"type": "message"}],
                      "incomplete_details": {"reason": "max_output_tokens"}}}]),
}


@pytest.mark.parametrize("model_id", sorted(STREAM_ERRORS))
def test_a_failure_reported_inside_the_stream_is_not_a_reply(streams, env, sse,
                                                             models, model_id):
    sse.events = STREAM_ERRORS[model_id]

    with pytest.raises(DicError, match="overloaded"):
        say(streams, env, "hi", model=model_id)

    row = tree(env)[0]
    assert row["status"] == -1 and "overloaded" in row["error"]
    assert row["response"] == "par"          # what did arrive is still kept
    assert not os.path.exists(session_path(env))    # and never continued


def test_a_stream_cut_before_its_terminal_event_is_not_a_reply(
        streams, env, sse, model):
    """A stream that stopped without [DONE] is a fragment, not an answer.

    A proxy that closes the connection mid-stream leaves dic with a partial
    answer and no terminal event, and before this it wrote a 200 for it: the
    row was continued by -c and the fragment was priced as a whole call.
    """
    sse.events = [{"choices": [{"delta": {"reasoning_content": "hmm"}}]}]

    with pytest.raises(DicError, match="terminal event"):
        say(streams, env, "hi", model=model)

    row = tree(env)[0]
    assert row["status"] == -1 and "terminal event" in row["error"]
    assert not os.path.exists(session_path(env))    # and never continued


@pytest.mark.parametrize("model_id", sorted(TRUNCATED))
def test_a_truncated_answer_says_so_and_is_still_an_answer(streams, env, sse,
                                                           models, model_id):
    reason, events = TRUNCATED[model_id]
    streams.err.tty = True                   # verbosity 1: the notice is level 1
    sse.events = events

    reply = say(streams, env, "hi", model=model_id)

    assert reply.status == 200
    assert streams.out.getvalue() == "half\n"
    assert f"truncated: {reason}" in streams.err.getvalue()


def test_unparsable_tool_arguments_do_not_kill_the_call(streams, env, sse, models):
    sse.events = [
        {"type": "content_block_start",
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f"}},
        {"type": "content_block_delta",
         "delta": {"type": "input_json_delta", "partial_json": '{"a"'}},
        {"type": "content_block_stop"},
        {"type": "message_stop"}]

    say(streams, env, "hi", model="fake+anthropic")

    (block,) = json.loads(tree(env)[0]["response_raw"])
    assert (block["type"], block["input"]) == ("tool_use", {})


def test_a_tool_call_that_never_finished_is_stored_without_its_scratch_key(
        streams, env, sse, models):
    sse.events = [
        {"type": "content_block_start",
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f"}},
        {"type": "content_block_delta",
         "delta": {"type": "input_json_delta", "partial_json": '{"a": 1}'}},
        {"type": "message_stop"}]

    say(streams, env, "hi", model="fake+anthropic")

    (block,) = json.loads(tree(env)[0]["response_raw"])
    assert "_json" not in block and block["input"] == {}


def test_a_cache_the_model_prices_goes_on_the_wire_and_the_row(streams, env, sse,
                                                                models):
    sse.events = [
        {"type": "message_start",
         "message": {"usage": {"input_tokens": 1000,
                               "cache_creation_input_tokens": 200,
                               "output_tokens": 0}}},
        {"type": "content_block_start",
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "content_block_stop"},
        {"type": "message_delta", "usage": {"output_tokens": 500}},
        {"type": "message_stop"}]

    say(streams, env, "hi", model="fake+anthropic",
        system="be brief", cache="5m")

    # the breakpoint is on the system prompt and the last turn, which is what
    # -c reuses, and it is the ephemeral 5m the price table rates
    body = sse.received[-1].body
    assert body["system"] == [{"type": "text", "text": "be brief",
                               "cache_control": {"type": "ephemeral"}}]
    assert body["messages"][-1]["content"][-1]["cache_control"] == {
        "type": "ephemeral"}
    # and the write side the response reported lands under the TTL's own name,
    # so the cache write is priced by the rule that rates it
    assert json.loads(tree(env)[0]["usage"]) == {
        "in": 1000, "in.cache_write.5m": 200, "out": 500}


def test_a_cache_ttl_the_model_does_not_price_fails_before_the_network(
        streams, env, sse, models):
    with pytest.raises(DicError, match="5m"):
        say(streams, env, "hi", model="fake+anthropic", cache="1h")

    assert sse.received == []          # before the network, not after it


def test_a_replay_does_not_replay_a_breakpoint(streams, env, sse, models, events):
    sse.events = events("first", "anthropic-messages")
    first = say(streams, env, "one", model="fake+anthropic",
                system="be brief", cache="5m")
    sse.events = events("second", "anthropic-messages")
    say(streams, env, "two", model="fake+anthropic",
        system="be brief", cache="5m", mid=first.mid)

    # a breakpoint belongs to one request and is copied in, so what got stored
    # is what the provider sent -- and carries none of it
    assert "cache_control" not in tree(env)[0]["response_raw"]

    # and the second request marks the new last turn, not the first one it
    # replayed: the history goes out as it was recorded
    body = sse.received[-1].body
    assert body["messages"][0]["content"] == [{"type": "text", "text": "one"}]
    assert body["messages"][1]["content"] == [{"type": "text",
                                               "text": "first"}]
    assert body["messages"][-1]["content"][-1]["cache_control"] == {
        "type": "ephemeral"}


def test_the_response_tier_prices_the_call_not_the_request(streams, env, sse, model):
    sse.events = [
        {"choices": [{"delta": {"content": "hi"}}], "service_tier": "default"},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 1000,
                                  "completion_tokens": 500}},
        "[DONE]"]

    say(streams, env, "hi", model=model, option=["service_tier=batch"])

    # the request asked for batch and the response says it charged default:
    # the default rate is what the row was billed, because a provider that
    # ignores -o must not be priced as though it had obeyed it
    assert tree(env)[0]["cost"] == pytest.approx(0.0105)
