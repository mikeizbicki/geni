"""The tree a file makes, and the hash that names it."""
from accio.extract import identify, lines


def test_identify_is_stable():
    a = identify("a.py", "x = 1\n")
    b = identify("a.py", "x = 1\n")
    assert a == b


def test_identify_depends_on_text():
    assert identify("a.py", "x = 1\n") != identify("a.py", "x = 2\n")


def test_identify_depends_on_breadcrumb():
    assert identify("a.py", "x = 1\n") != identify("b.py", "x = 1\n")


def test_identify_is_16_hex():
    h = identify("a.py", "x = 1\n")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_lines_is_one_root():
    chunks = lines("a.py", "x = 1\n")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.path == "a.py"
    assert c.breadcrumb == "a.py"
    assert c.kind == "file"
    assert c.ord == 0
    assert c.parent is None
    assert c.chunk_id == identify("a.py", "x = 1\n")


def test_lines_hash_is_content_addressed():
    # a rename costs one new path on a chunk that is otherwise the same
    a = lines("a.py", "x = 1\n")[0].chunk_id
    b = lines("b.py", "x = 1\n")[0].chunk_id
    assert a != b
    # but two different files with the same breadcrumb and text do not
    assert lines("a.py", "x = 1\n")[0].chunk_id == a
