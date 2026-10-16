"""The Python extractor and the body elision it exists for."""
from accio.extract import by_extension, python
from accio.render import elide


SRC = (
    '"""Module."""\n'
    "\n"
    "import os\n"
    "\n"
    "\n"
    "def top(a, b):\n"
    '    """Return a + b."""\n'
    "    return a + b\n"
    "\n"
    "\n"
    "class Greeter:\n"
    '    """Says hello."""\n'
    "\n"
    "    def greet(self, name):\n"
    '        return "hello " + name\n'
)


def test_python_kinds():
    kinds = [c.kind for c in python("a.py", SRC)]
    assert kinds[0] == "file"
    assert "def" in kinds
    assert "class" in kinds
    assert "docstring" in kinds


def test_python_breadcrumbs():
    crumbs = [c.breadcrumb for c in python("a.py", SRC)]
    assert "a.py" in crumbs
    assert "a.py::top" in crumbs
    assert "a.py::Greeter" in crumbs
    assert "a.py::Greeter::greet" in crumbs
    assert "a.py::top#doc" in crumbs
    assert "a.py::Greeter#doc" in crumbs


def test_python_parent_edges():
    cs = {c.breadcrumb: c for c in python("a.py", SRC)}
    assert cs["a.py::top"].parent == cs["a.py"].chunk_id
    assert cs["a.py::Greeter"].parent == cs["a.py"].chunk_id
    assert cs["a.py::Greeter::greet"].parent == cs["a.py::Greeter"].chunk_id
    assert cs["a.py::top#doc"].parent == cs["a.py::top"].chunk_id
    assert cs["a.py"].parent is None


def test_python_ids_are_content_addressed():
    a = {c.breadcrumb: c.chunk_id for c in python("a.py", SRC)}
    b = {c.breadcrumb: c.chunk_id for c in python("a.py", SRC)}
    assert a == b


def test_python_file_chunk_is_module_level():
    file_chunk = python("a.py", SRC)[0]
    assert "import os" in file_chunk.text
    assert "Module." in file_chunk.text
    assert "def top" not in file_chunk.text
    assert "class Greeter" not in file_chunk.text


def test_python_def_chunk_is_the_whole_definition():
    cs = {c.breadcrumb: c for c in python("a.py", SRC)}
    assert "def top(a, b):" in cs["a.py::top"].text
    assert "return a + b" in cs["a.py::top"].text


def test_python_syntax_error_falls_back_to_lines():
    chunks = python("bad.py", "def ( :\n")
    assert len(chunks) == 1
    assert chunks[0].kind == "file"


def test_by_extension_routes_py_and_the_rest():
    a = by_extension("a.py", "def f():\n    pass\n")
    assert [c.kind for c in a] == ["file", "def"]
    b = by_extension("a.txt", "hello\n")
    assert [c.kind for c in b] == ["file"]


def test_elide_body_keeps_signature_and_docstring():
    cs = [c for c in python("a.py", SRC) if c.breadcrumb == "a.py::top"]
    [elided] = list(elide(cs, "body"))
    assert "def top(a, b):" in elided.text
    assert '"""Return a + b."""' in elided.text
    assert "return a + b" not in elided.text


def test_elide_body_leaves_a_headless_chunk_alone():
    [only] = list(elide(python("a.py", "x = 1\n"), "body"))
    assert only.text == "x = 1\n"


def test_elide_body_preserves_ids_and_order():
    before = list(python("a.py", SRC))
    after = list(elide(before, "body"))
    assert [c.chunk_id for c in before] == [c.chunk_id for c in after]
    assert [c.breadcrumb for c in before] == [c.breadcrumb for c in after]


def test_elide_body_is_idempotent():
    once = list(elide(python("a.py", SRC), "body"))
    twice = list(elide(once, "body"))
    assert [c.text for c in once] == [c.text for c in twice]
