"""--schema, from the flag to the wire to the row.

schema.py's compilation is doctested where it lives.  What a doctest
cannot reach is a schema that reaches a provider: the request body each
protocol asks it in, the row the call leaves behind, and the answers
that repair and validation do and do not accept.  Every test here is one
dic() call against the fake server conftest.py brings.
"""
import json, os

import pytest

from dic.client import dic
from dic.store import db, session_path
from dic.tty import DicError


SCHEMA = ('{"type": "object", "properties": {"a": {"type": "integer"}},'
          ' "required": ["a"], "additionalProperties": false}')


def say(streams, env, prompt, **knobs):
    """One dic() call through the fake streams and the test environment."""
    return dic(prompt, env=env, out=streams.out, err=streams.err, **knobs)


def tree(env):
    """The message tree, oldest first."""
    conn = db(env)
    rows = conn.execute("SELECT * FROM messages ORDER BY t_start").fetchall()
    conn.close()
    return rows


# ----------------------------------------------------- the wire and the row


def test_a_json_schema_reaches_openai_chat_as_response_format(
        streams, env, sse, model, events):
    sse.events = events('{"a": 1}')
    say(streams, env, "hi", model=model, schema=SCHEMA)

    fmt = sse.received[-1].body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == json.loads(SCHEMA)


def test_a_json_schema_reaches_openai_responses_as_text_format(
        streams, env, sse, models):
    sse.events = [
        {"type": "response.output_text.delta", "delta": '{"a": 1}'},
        {"type": "response.completed",
         "response": {"output": [],
                      "usage": {"input_tokens": 1, "output_tokens": 1}}}]
    say(streams, env, "hi", model="fake+responses", schema=SCHEMA)

    fmt = sse.received[-1].body["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["schema"] == json.loads(SCHEMA)


def test_the_row_records_schema_dialect_mime_and_enforced(
        streams, env, sse, model, events):
    sse.events = events('{"a": 1}')
    say(streams, env, "hi", model=model, schema=SCHEMA)

    row = tree(env)[0]
    assert json.loads(row["schema"]) == json.loads(SCHEMA)
    assert row["dialect"] == "jsonschema"
    assert row["mime_type"] == "application/json"
    assert row["container"] is None
    assert row["repaired"] is None
    assert row["enforced"] == 1


def test_the_shorthand_compiles_and_reaches_the_wire(
        streams, env, sse, model, events):
    sse.events = events('{"name": "x"}')
    say(streams, env, "hi", model=model, schema="name")

    schema = sse.received[-1].body["response_format"]["json_schema"]["schema"]
    assert schema == {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"],
                      "additionalProperties": False}
    assert tree(env)[0]["dialect"] == "simonw_llm"


def test_a_printed_request_carries_the_directive(streams, env, sse, model):
    say(streams, env, "hi", model=model, schema=SCHEMA, print_="json")

    printed = streams.out.getvalue()
    assert '"json_schema"' in printed
    assert '"strict": true' in printed
    assert sse.received == []


# --------------------------------------------------------------- inheritance


def test_a_continue_inherits_the_row_s_schema(
        streams, env, sse, model, events):
    sse.events = events('{"name": "a"}')
    say(streams, env, "one", model=model, schema="name")
    sse.events = events('{"name": "b"}')
    say(streams, env, "two", model=model, cont=True)

    schema = sse.received[-1].body["response_format"]["json_schema"]["schema"]
    assert schema["properties"] == {"name": {"type": "string"}}
    row = tree(env)[1]
    assert json.loads(row["schema"]) == schema
    assert row["dialect"] == "simonw_llm"


# --------------------------------------------------------------- the mimes

def test_a_json_mime_without_a_schema_is_refused(streams, env, sse, model):
    # a JSON mime promises an answer is JSON and that it is the one asked
    # for, and a schema is the only thing that says either; no provider
    # takes a JSON mime without one, so it is refused before the network
    with pytest.raises(DicError, match="needs --schema"):
        say(streams, env, "hi", model=model, mime_type="application/json")
    assert sse.received == []


def test_a_jsonl_mime_without_a_schema_is_refused(streams, env, sse, model):
    with pytest.raises(DicError, match="needs --schema"):
        say(streams, env, "hi", model=model, mime_type="application/x-ndjson")
    assert sse.received == []


def test_a_manual_text_mime_drops_the_schema(
        streams, env, sse, model, events):
    sse.events = events("prose")
    say(streams, env, "hi", model=model, schema=SCHEMA, mime_type="text/plain")

    assert "response_format" not in sse.received[-1].body
    row = tree(env)[0]
    assert row["schema"] is None
    assert row["mime_type"] == "text/plain"


def test_a_non_json_mime_with_a_schema_is_an_error(streams, env, sse, model):
    with pytest.raises(DicError, match="contradicts a schema"):
        say(streams, env, "hi", model=model, schema=SCHEMA,
            mime_type="image/png")
    assert sse.received == []


def test_extract_and_a_json_mime_are_an_error(streams, env, sse, model):
    with pytest.raises(DicError, match="-x and a JSON mime"):
        say(streams, env, "hi", model=model, schema=SCHEMA, extract=True)
    assert sse.received == []


# --------------------------------------------------------------- dialects


def test_systemone_in_a_text_protocol_is_refused(streams, env, sse, model):
    with pytest.raises(DicError, match="only for typesafe"):
        say(streams, env, "hi", model=model, schema='billing: noul "?"')
    assert sse.received == []


def test_jsonschema_on_typesafe_is_refused(streams, env, models):
    with pytest.raises(DicError, match="is not its dialect"):
        dic("hi", model="fake+typesafe", schema=SCHEMA,
            env=env, out=streams.out, err=streams.err)


def test_typesafe_requires_a_schema(streams, env, models):
    with pytest.raises(DicError, match="typesafe requires --schema"):
        dic("hi", model="fake+typesafe",
            env=env, out=streams.out, err=streams.err)


def test_typesafe_refuses_o_questions_beside_a_schema(streams, env, models):
    with pytest.raises(DicError, match="pick one"):
        dic("hi", model="fake+typesafe", schema='billing: noul "?"',
            option=["questions={}"],
            env=env, out=streams.out, err=streams.err)


def test_typesafe_refuses_a_jsonl_mime(streams, env, models):
    with pytest.raises(DicError, match="not a typesafe shape"):
        dic("hi", model="fake+typesafe", schema='billing: noul "?"',
            mime_type="application/x-ndjson",
            env=env, out=streams.out, err=streams.err)


def test_repair_on_where_the_protocol_cannot_is_an_error(streams, env, models):
    with pytest.raises(DicError, match="cannot repair"):
        dic("hi", model="fake+typesafe", schema='billing: noul "?"',
            repair="on", env=env, out=streams.out, err=streams.err)


def test_an_adaptor_without_output_format_refuses_the_schema(
        streams, env, sse, models):
    with pytest.raises(DicError, match="output_format"):
        dic("hi", model="fake+images", schema=SCHEMA,
            env=env, out=streams.out, err=streams.err)
    assert sse.received == []


def test_continue_on_a_stateless_backend_is_refused(
        streams, env, sse, model, events):
    sse.events = events("hello")
    say(streams, env, "one", model=model)

    with pytest.raises(DicError, match="no conversation"):
        say(streams, env, "two", model="fake+images", cont=True)


# ---------------------------------------------------------------- repair


def test_repair_recovers_a_fenced_answer(streams, env, sse, model, events):
    sse.events = events('```json\n{"a": 1}\n```')
    say(streams, env, "hi", model=model, schema=SCHEMA)

    assert tree(env)[0]["repaired"] == "code_block"


def test_repair_off_leaves_the_fence(streams, env, sse, model, events):
    sse.events = events('```json\n{"a": 1}\n```')
    with pytest.raises(DicError, match="validation failed"):
        say(streams, env, "hi", model=model, schema=SCHEMA, repair="off")

    assert tree(env)[0]["repaired"] is None


# --------------------------------------------------------------- validate


def test_a_valid_answer_is_stored_and_exits_zero(
        streams, env, sse, model, events):
    sse.events = events('{"a": 1}')
    reply = say(streams, env, "hi", model=model, schema=SCHEMA)

    assert reply.status == 200
    assert streams.out.getvalue() == '{"a": 1}\n'


def test_a_validation_failure_prints_the_answer_and_moves_the_pointer(
        streams, env, sse, model, events):
    sse.events = events('{"a": "not an integer"}')
    with pytest.raises(DicError, match="validation failed"):
        say(streams, env, "hi", model=model, schema=SCHEMA)

    # the answer is printed so the model can read it and correct itself
    assert '{"a": "not an integer"}' in streams.out.getvalue()
    # and the session pointer moved, so -c replays the bad row
    assert os.path.exists(session_path(env))


def test_validate_off_accepts_a_bad_answer(streams, env, sse, model, events):
    sse.events = events('{"a": "not an integer"}')
    reply = say(streams, env, "hi", model=model, schema=SCHEMA,
                validate="off")

    assert reply.status == 200


def test_a_remote_ref_is_refused(streams, env, sse, model, events):
    remote = ('{"type": "object", "properties":'
              ' {"a": {"$ref": "https://example.com/s.json"}}}')
    sse.events = events('{"a": 1}')
    with pytest.raises(DicError, match="remote \\$ref"):
        say(streams, env, "hi", model=model, schema=remote)


# ---------------------------------------------------------------- JSONL


def test_a_jsonl_mime_wraps_the_schema_and_records_the_container(
        streams, env, sse, model, events):
    # every provider requires a schema's root to be an object and none
    # accepts a top-level array, so the records are asked for in one
    sse.events = events('{"jsonl": [{"a": 1}, {"a": 2}]}')
    say(streams, env, "hi", model=model, schema=SCHEMA,
        mime_type="application/x-ndjson",
        repair="off", validate="off")

    wire = sse.received[-1].body["response_format"]["json_schema"]["schema"]
    assert wire["type"] == "object"
    assert wire["properties"]["jsonl"]["items"] == json.loads(SCHEMA)

    row = tree(env)[0]
    assert row["container"] == 1
    assert row["mime_type"] == "application/x-ndjson"
    # the caller's own schema is what the row keeps, not the wire wrap
    assert json.loads(row["schema"]) == json.loads(SCHEMA)
    # and stdout is one record per line, which is what the mime asked for
    assert streams.out.getvalue() == '{"a": 1}\n{"a": 2}\n'


# ---------------------------------------------------------------- anthropic


def test_anthropic_carries_the_schema_as_a_forced_tool(streams, env, sse,
                                                       models):
    sse.events = [
        {"type": "message_start",
         "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "content_block_start",
         "content_block": {"type": "tool_use", "id": "t1",
                           "name": "output"}},
        {"type": "content_block_delta",
         "delta": {"type": "input_json_delta",
                   "partial_json": '{"a": 1}'}},
        {"type": "content_block_stop"},
        {"type": "message_delta", "usage": {"output_tokens": 5}}]
    say(streams, env, "hi", model="fake+anthropic", schema=SCHEMA)

    body = sse.received[-1].body
    assert body["tool_choice"] == {"type": "tool", "name": "output"}
    assert [t["name"] for t in body["tools"]] == ["output"]
    assert body["tools"][0]["input_schema"] == json.loads(SCHEMA)
    # the answer comes out of the tool_use block as an object
    assert streams.out.getvalue() == '{"a": 1}\n'
    row = tree(env)[0]
    assert row["enforced"] == 0
    (block,) = json.loads(row["response_raw"])
    assert (block["type"], block["name"]) == ("tool_use", "output")
