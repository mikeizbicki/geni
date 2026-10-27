"""The four projections describe the same selection."""
import io
import json

import pytest

from accio.extract import lines
from accio.render import FORMATS, budget, measure, parse_target


CHUNKS = lines("a.py", "x = 1\n") + lines("b.txt", "hello world\n")


def _run(fmt):
    buf = io.StringIO()
    FORMATS[fmt](CHUNKS, buf)
    return buf.getvalue()


def _paths_from_prompt(out):
    result = []
    rows = out.splitlines()
    i = 0
    while i < len(rows):
        result.append(rows[i])
        assert rows[i + 1] == "---"
        i += 2
        while i < len(rows) and rows[i] != "---":
            i += 1
        i += 1
    return set(result)


def _paths_from(fmt):
    out = _run(fmt)
    if fmt == "prompt":
        return _paths_from_prompt(out)
    if fmt == "paths":
        return set(out.splitlines())
    if fmt == "json":
        return {json.loads(row)["path"]
                for row in out.splitlines() if row}
    if fmt == "wc":
        return {row.rsplit(None, 1)[-1]
                for row in out.splitlines()
                if row and not row.endswith("total")
                and not row.startswith("~")}
    if fmt == "at-paths":
        return {row[2:] for row in out.splitlines() if row}
    raise AssertionError(fmt)


def test_formats_agree_on_paths():
    expected = {"a.py", "b.txt"}
    for fmt in FORMATS:
        assert _paths_from(fmt) == expected, fmt


def test_formats_each_render_every_chunk():
    for fmt in FORMATS:
        out = _run(fmt)
        assert out, fmt


def test_at_paths_is_a_dic_prompt():
    # the round trip that makes --format at-paths worth having: the bytes
    # at_paths writes are the bytes dic.include scans back into a
    # filename, so a walk piped into dic arrives as declared mentions
    # and not as prose to be guessed at.
    from accio.include import scan
    _clean, spans = scan(_run("at-paths"))
    assert [key for _start, _end, _origin, key in spans] == \
        [c.path for c in CHUNKS]


def test_json_is_one_object_per_line():
    out = _run("json")
    assert out.endswith("\n")
    rows = [row for row in out.splitlines() if row]
    assert len(rows) == len(CHUNKS)
    for row in rows:
        obj = json.loads(row)
        assert set(obj) == {"path", "chunk_id", "breadcrumb", "kind",
                            "ord", "text"}
        assert obj["kind"] == "file"
        assert len(obj["chunk_id"]) == 16


def test_render_fences_every_chunk():
    out = _run("prompt")
    assert out.endswith("---\n")
    assert out.count("---\n") == 2 * len(CHUNKS)


def test_render_writes_newline_below_text():
    # a file whose last line has no newline must not run into the fence
    buf = io.StringIO()
    FORMATS["prompt"](lines("a.txt", "no newline"), buf)
    assert buf.getvalue() == "a.txt\n---\nno newline\n---\n"


def test_wc_totals():
    out = _run("wc")
    rows = out.splitlines()
    # the second to last row is the total
    total = [int(x) for x in rows[-2].split()[:3]]
    assert total == [8, 11, 45]


def test_measure_counts_the_rendered_stream():
    c = lines("a.py", "x = 1\n")[0]
    # "a.py\n---\nx = 1\n---\n"
    assert measure(c, "bytes") == 19
    assert measure(c, "lines") == 4
    assert measure(c, "words") == 6
    assert measure(c, "tokens") == 4
    assert measure(c, "chunks") == 1


def test_measure_refuses_an_unknown_unit():
    c = lines("a.py", "x = 1\n")[0]
    with pytest.raises(ValueError):
        measure(c, "furlongs")


@pytest.mark.parametrize("text,expected", [
    ("8192", (8192, "bytes")),
    ("8k", (8192, "bytes")),
    ("8kB", (8192, "bytes")),
    ("8kT", (8192, "tokens")),
    ("8kW", (8192, "words")),
    ("200L", (200, "lines")),
    ("50C", (50, "chunks")),
    ("1m", (1024 * 1024, "bytes")),
])
def test_parse_target(text, expected):
    assert parse_target(text) == expected


@pytest.mark.parametrize("text", ["", "k", "eight", "8q", "8kb", "-1"])
def test_parse_target_rejects_junk(text):
    with pytest.raises(ValueError):
        parse_target(text)


def _pair():
    return (lines("a.py", "x = 1\n")[0], lines("b.py", "y = 2\n")[0])


def test_budget_always_emits_the_top_chunk():
    a, _ = _pair()
    assert len(list(budget([a], 1, "bytes", 0))) == 1


def test_budget_stops_at_the_crossing():
    a, b = _pair()
    assert [c.path for c in budget([a, b], 20, "bytes", 0)] == ["a.py"]


def test_budget_fudge_admits_the_crossing():
    a, b = _pair()
    # 19 + 19 = 38 against a target of 35: three over, inside a quarter
    assert [c.path for c in budget([a, b], 35, "bytes", 0)] == ["a.py"]
    assert [c.path for c in budget([a, b], 35, "bytes", 0.25)] == [
        "a.py", "b.py"]


def test_budget_counts_chunks():
    a, b = _pair()
    assert [c.path for c in budget([a, b], 1, "chunks", 0)] == ["a.py"]
    assert [c.path for c in budget([a, b], 2, "chunks", 0)] == [
        "a.py", "b.py"]
