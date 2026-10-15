"""Rendering: the selected nodes, as the one format accio has.

A node is a path, a `---` fence, its text and a closing fence, so a reader
-- a model, a human, `less` -- can see where one file stops, and so that
the output is the encoding files-to-prompt already produces.  `paths` and
`wc` are projections of the same selection: the selection decides what,
and this module decides only how.
"""


def render(nodes, out):
    """Write selected nodes as the one format accio has: path, fences, text.

    The path is on a line of its own, a `---` fence follows it, the text
    follows that, and a closing `---` follows the text, so that a reader
    -- a model, a human, `less` -- can see where one file stops, and so
    that the output is the encoding files-to-prompt already produces.  A
    fence closes every node, so the last line of the whole run is always
    `---`.  The newline below the text is written and never assumed: a
    file whose last line has no newline of its own must not run into the
    closing fence.

    >>> import io
    >>> from accio.traverse import Node
    >>> stream = io.StringIO()
    >>> render([Node("a.py", "x = 1\\n")], stream)
    >>> stream.getvalue()
    'a.py\\n---\\nx = 1\\n---\\n'
    >>> stream = io.StringIO()
    >>> render([Node("b.txt", "no newline")], stream)
    >>> stream.getvalue()
    'b.txt\\n---\\nno newline\\n---\\n'
    """
    for node in nodes:
        out.write(f"{node.path}\n---\n{node.text}")
        if not node.text.endswith("\n"):
            out.write("\n")
        out.write("---\n")


def paths(nodes, out):
    """Write the path of each selected node, one per line, and no contents.

    >>> import io
    >>> from accio.traverse import Node
    >>> stream = io.StringIO()
    >>> paths([Node("a.py", "x = 1\\n"), Node("b.txt", "y")], stream)
    >>> print(stream.getvalue(), end="")
    a.py
    b.txt
    """
    for node in nodes:
        out.write(f"{node.path}\n")


def wc(nodes, out):
    """Write selected nodes as `wc` does, and one token estimate.

    Lines are newlines, words are whitespace-separated runs, and bytes
    are the text as utf-8 -- the three numbers `wc -l -w -c` reports, in
    order, one file to a line, then a `total` line for the run.  Ancestor
    context files are counted as the paths they are, because that is what
    they cost the prompt.

    The token count is not knowable here: it depends on the tokenizer and
    the model, and neither is accio's business.  `bytes/4` is the usual
    rule of thumb for English and is exact on nothing; it is printed as
    an estimate, marked with `~`, and is a proxy for size rather than a
    prediction of cost.

    >>> import io
    >>> from accio.traverse import Node
    >>> stream = io.StringIO()
    >>> wc([Node("a.py", "x = 1\\n"), Node("b.txt", "no newline")], stream)
    >>> print(stream.getvalue(), end="")
          1       3       6 a.py
          0       2      10 b.txt
          1       5      16 total
    ~4 tokens (bytes/4)
    """
    lines = words = size = 0
    for node in nodes:
        n = node.text.count("\n")
        w = len(node.text.split())
        b = len(node.text.encode("utf-8"))
        out.write(f"{n:7d} {w:7d} {b:7d} {node.path}\n")
        lines += n
        words += w
        size += b
    out.write(f"{lines:7d} {words:7d} {size:7d} total\n")
    out.write(f"~{size // 4} tokens (bytes/4)\n")
