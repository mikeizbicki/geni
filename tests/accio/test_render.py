"""The four projections describe the same selection."""
import io
import json

from accio.extract import lines
from accio.render import FORMATS


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
    raise AssertionError(fmt)


def test_formats_agree_on_paths():
    expected = {"a.py", "b.txt"}
    for fmt in FORMATS:
        assert _paths_from(fmt) == expected, fmt


def test_formats_each_render_every_chunk():
    for fmt in FORMATS:
        out = _run(fmt)
        assert out, fmt


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
    assert total == [2, 5, 18]
