"""Where the answer goes: a file written atomically, or the terminal.

`--path` names the file the answer is written to.  It is written beside
itself as `NAME.part` and renamed into place, so a reader sees a whole
answer or none, a cancelled call leaves the real path untouched, and the
leftover `.part` is named in the error.

Text is one file at the name given.  A blob is bytes a terminal cannot
show, so it has no terminal form: dic refuses to write one without --path,
and writes one numbered file per blob, `NAME-0.mp4`, because a stream does
not say in advance how many it will carry.

An existing file is a failure before the call is made -- a stale redirect
must never be silently extended -- and a file that appears while the call
is running is warned about and left alone, so dic overwrites nothing the
user did not ask it to with -f.
"""
import os, sys

from dic.tty import DicError, report


def numbered(path, i):
    """The i-th blob's path: the number goes before the extension.

    >>> numbered("panda.mp4", 0), numbered("a/b.png", 3)
    ('panda-0.mp4', 'a/b-3.png')
    """
    root, ext = os.path.splitext(path)
    return f"{root}-{i}{ext}"


def check_free(path, force=False):
    """Fail, before the call is made, if the output file is already there.

    >>> check_free(None)          # stdout is not a file and is always free
    """
    if path and os.path.exists(path) and not force:
        raise DicError(f"{path} already exists (use -f to overwrite)")


class Sink:
    """The one place a byte of a reply reaches a file or the terminal.

    A text answer goes to stdout, or to --path as `PATH.part` renamed when
    the sink closes, so the file appears whole or not at all.  A blob goes
    to a numbered --path, one file per blob, and only where --path was
    given: bytes are not a terminal's business.
    """

    def __init__(self, path=None, force=False, out=None, err=None, env=None,
                 verbosity=1):
        self.path = path
        self.force = force
        self.out = sys.stdout if out is None else out
        self.err = err
        self.env = env
        self.verbosity = verbosity
        self.file = None            # the text .part, once there is text
        self.blob = None            # the current blob's .part
        self.blob_path = None
        self.n = 0
        self.paths = []             # blob files completed, in order

    def write(self, text):
        """A text delta: the terminal, or the text file opened lazily."""
        if self.path is None:
            self.out.write(text)
            self.out.flush()
            return
        if self.file is None:
            self.file = open(f"{self.path}.part", "w")  # noqa: SIM115
        self.file.write(text)
        self.file.flush()

    def write_blob(self, data):
        """A blob delta: appended to the current blob, opened lazily."""
        if self.blob is None:
            self.blob_path = self.free_path()
            self.blob = open(f"{self.blob_path}.part", "wb")  # noqa: SIM115
        self.blob.write(data)
        self.blob.flush()

    def free_path(self):
        """The next blob name, skipping any that appeared while we ran."""
        while True:
            path = numbered(self.path, self.n)
            self.n += 1
            if self.force or not os.path.exists(path):
                return path
            report(self.verbosity, 1, f"{path} exists; not overwriting",
                   err=self.err, env=self.env)

    def commit(self, path):
        """Rename path.part to path, unless something took the name meanwhile."""
        part = f"{path}.part"
        if not self.force and os.path.exists(path):
            report(self.verbosity, 1, f"{path} appeared; left as {part}",
                   err=self.err, env=self.env)
            return part
        os.replace(part, path)
        return path

    def end_blob(self):
        """Finish the current blob, renaming it into place, if there is one."""
        if self.blob is None:
            return
        self.blob.close()
        self.blob = None
        self.paths.append(self.commit(self.blob_path))

    def close(self):
        """End the last blob and rename the text file; the blob paths written."""
        self.end_blob()
        if self.file is not None:
            self.file.close()
            self.file = None
            self.commit(self.path)
        return self.paths

    def pending(self):
        """The `.part` this call has open, or None: what a cancel leaves behind."""
        if self.blob is not None:
            return f"{self.blob_path}.part"
        if self.file is not None:
            return f"{self.path}.part"
        return None

    def abandon(self):
        """Close without renaming: a cancelled call keeps its .part as evidence."""
        if self.blob is not None:
            self.blob.close()
            self.blob = None
        if self.file is not None:
            self.file.close()
            self.file = None


class Reindent:
    """A streaming reindenter for one JSON answer: text in, pretty text out.

    A JSON answer is printed as it arrives, so reindenting it cannot mean
    feeding the whole document to json.dumps.  It means a DFA over the
    characters instead: a stack of the containers still open and whether
    each is empty, a flag for the string state, and the separator the next
    token needs.  The document is already well-formed -- the provider was
    asked with a strict schema -- so nothing here parses a value; it only
    moves whitespace.

    Everything before the first brace is skipped, so a fenced answer is
    reindented without being stripped first: the fence is prose, and prose
    is not JSON.  The trailing newline the answer needs is what close()
    writes, because whitespace between tokens is what the DFA drops.

    >>> p = Reindent()
    >>> p.feed('{"a": [1, ') + p.feed('2]}') + p.close()
    '{\\n  "a": [\\n    1,\\n    2\\n  ]\\n}\\n'
    >>> p = Reindent()
    >>> p.feed('```json\\n{"a": true}\\n```') + p.close()
    '{\\n  "a": true\\n}\\n'
    >>> Reindent().close()
    ''
    """

    def __init__(self):
        self.stack = []          # True while the container still holds nothing
        self.in_string = False
        self.escape = False
        self.started = False     # the first `{` or `[` has arrived
        self.pending = ""        # what the next value token is preceded by

    def feed(self, text):
        """Reindent one more piece of the answer and return what to print."""
        out = []
        for ch in text:
            self._step(ch, out)
        return "".join(out)

    def close(self):
        """The newline the answer ends with, or nothing if none began."""
        return "\n" if self.started else ""

    def _step(self, ch, out):
        if self.in_string:
            out.append(ch)
            if self.escape:
                self.escape = False
            elif ch == "\\":
                self.escape = True
            elif ch == '"':
                self.in_string = False
            return
        if self.started and not self.stack:
            return              # the document ended at its outermost closer
        if not self.started:
            if ch in "{[":
                self.started = True
                out.append(ch)
                self.stack.append(True)
                self.pending = "\n  "
            return
        if ch == '"':
            self._value(out)
            self.in_string = True
            out.append(ch)
        elif ch in "{[":
            self._value(out)
            out.append(ch)
            self.stack.append(True)
            self.pending = "\n" + "  " * len(self.stack)
        elif ch in "}]":
            if not self.stack.pop():
                out.append("\n" + "  " * len(self.stack))
            out.append(ch)
        elif ch == ",":
            out.append(ch)
            self.pending = "\n" + "  " * len(self.stack)
        elif ch == ":":
            out.append(": ")
            self.pending = ""
        elif ch not in " \t\n\r":
            self._value(out)
            out.append(ch)

    def _value(self, out):
        """Put the separator the next value needs in front of it."""
        out.append(self.pending)
        self.pending = ""
        if self.stack:
            self.stack[-1] = False
