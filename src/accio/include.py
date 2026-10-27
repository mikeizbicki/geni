r"""Include directives for dic prompts.

A prompt may name files with @@path and run commands with %% at the start
of a line. dic hoists each named file and each command's stdout into a
header block at the head of the request and leaves the prose alone. This
module owns the scanner: finding the mentions and consuming the \@@ and
\%% escapes that spell a literal @@ and %%.
"""

import hashlib, os

from accio.traverse import SNIFF


def scan(text):
    r"""Find @@path and %%command mentions in *text*.

    Returns ``(clean, spans)``. ``clean`` is *text* with every ``\@@`` and
    ``\%%`` rewritten to the two characters they escape, since the
    backslash is dic's escape and must not reach the model. ``spans`` is
    an ordered list of ``(start, end, origin, key)`` for each mention found
    in ``clean``, in first-occurrence order; a mention repeated is reported
    twice, because the caller decides what to do about the duplicate.

    A mention begins at a ``@@`` at the start of the string or just after a
    whitespace character, and its path runs to the next whitespace. No
    punctuation is trimmed, so ``see @@notes.md.`` names ``notes.md.``. A
    bare ``@@`` at end of input, or one followed by whitespace, is not a
    mention.

    A command mention begins at a ``%%`` at the start of a line -- column 0
    of the string, or the first character after a newline -- and its
    command runs to the end of that line, with the leading whitespace
    stripped. A ``%%`` anywhere else is prose, and a ``%%`` with an empty
    command is an error, because the caller meant to write one.

    Neither mention is rescanned: an ``@@`` or ``%%`` in a path or a
    command is literal text, and a header built from one is never scanned
    again.

    >>> scan("@@a.py")
    ('@@a.py', [(0, 6, 'file', 'a.py')])
    >>> scan("see @@a.py")
    ('see @@a.py', [(4, 10, 'file', 'a.py')])
    >>> scan("@@a @@b")
    ('@@a @@b', [(0, 3, 'file', 'a'), (4, 7, 'file', 'b')])
    >>> scan("\\@@a")
    ('@@a', [])
    >>> scan("@@")
    ('@@', [])
    >>> scan("see @@notes.md.")
    ('see @@notes.md.', [(4, 15, 'file', 'notes.md.')])
    >>> scan("%%echo hi")
    ('%%echo hi', [(0, 9, 'cmd', 'echo hi')])
    >>> scan("%% echo hi")
    ('%% echo hi', [(0, 10, 'cmd', 'echo hi')])
    >>> scan("x %%echo")
    ('x %%echo', [])
    >>> scan("\\%%echo")
    ('%%echo', [])
    >>> scan("%%echo\n%%also")
    ('%%echo\n%%also', [(0, 6, 'cmd', 'echo'), (7, 13, 'cmd', 'also')])
    >>> scan("%%echo @@a")
    ('%%echo @@a', [(0, 10, 'cmd', 'echo @@a')])
    >>> scan("%%")
    Traceback (most recent call last):
        ...
    dic.tty.DicError: %%: an empty command
    >>> scan("hello world")
    ('hello world', [])
    """
    clean = []
    spans = []
    pos = 0
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("\\@@", i):
            clean.append("@@")
            pos += 2
            i += 3
            continue
        if text.startswith("\\%%", i):
            clean.append("%%")
            pos += 2
            i += 3
            continue
        if text.startswith("@@", i) and (i == 0 or text[i - 1].isspace()):
            j = i + 2
            k = j
            while k < n and not text[k].isspace():
                k += 1
            if k > j:
                clean.append(text[i:k])
                spans.append((pos, pos + k - i, "file", text[j:k]))
                pos += k - i
                i = k
                continue
        if text.startswith("%%", i) and (i == 0 or text[i - 1] == "\n"):
            j = i + 2
            k = j
            while k < n and text[k] != "\n":
                k += 1
            key = text[j:k].strip()
            if not key:
                from dic.tty import DicError
                raise DicError("%%: an empty command")
            clean.append(text[i:k])
            spans.append((pos, pos + k - i, "cmd", key))
            pos += k - i
            i = k
            continue
        clean.append(text[i])
        pos += 1
        i += 1
    return "".join(clean), spans


def collect(text, known=(), run=None):
    r"""Hoist every @@path and %%command mention into a header block.

    Returns ``(clean, header, adds)``. ``clean`` is *text* with the ``\@@``
    and ``\%%`` escapes consumed and nothing else changed. ``header`` is
    the concatenation, in first-mention order, of one block per newly
    included file and per newly run command, each rendered as
    ``key\n---\ntext\n---\n`` -- the files-to-prompt encoding the rest of
    accio already uses. A file's key is its path; a command's key is its
    command text, and its text is the command's stdout. ``adds`` is a list
    of records for everything this call added, so the caller can store them
    and pass them back as *known* on a later turn: a file adds
    ``{"path", "hash", "kind"}`` and a command adds
    ``{"origin": "cmd", "key", "hash", "kind": "text"}``.

    *known* is a collection of identities an ancestor turn already carried:
    a file's is the pair ``(path, sha256)``, and a command's is the triple
    ``("cmd", key, sha256)``. A mention whose identity one of those matches
    contributes neither a block nor an add, and one named twice in a call
    is likewise added once. The header is never rescanned: a ``@@`` or
    ``%%`` inside an included file's own bytes is literal text.

    A command is run by *run*, the executor the caller injects; it takes
    the command text and returns ``(returncode, stdout_bytes)``. The
    command's leading ``--flag`` and ``NAME=VALUE`` words are the sandbox's
    own and widen the jail for that one child. A non-zero exit is refused
    before any block is built, because there is no output worth sending.

    A path that cannot be the file the caller meant is refused loudly
    rather than guessed at: a missing path is told to write the ``\@@``
    escape, a directory is told to use ``$(accio ...)``, a dangling
    symlink is named as one, and a URL scheme is refused outright.

    >>> import os, tempfile, shutil, hashlib
    >>> old = os.getcwd()
    >>> d = tempfile.mkdtemp(); os.chdir(d)
    >>> _ = open("notes.md", "w").write("hello\n")
    >>> clean, header, adds = collect("see @@notes.md", set())
    >>> clean
    'see @@notes.md'
    >>> header
    'notes.md\n---\nhello\n\n---\n'
    >>> [(a["path"], a["kind"]) for a in adds]
    [('notes.md', 'text')]
    >>> adds[0]["hash"] == hashlib.sha256(b"hello\n").hexdigest()
    True
    >>> known = {("notes.md", hashlib.sha256(b"hello\n").hexdigest())}
    >>> collect("see @@notes.md", known)
    ('see @@notes.md', '', [])
    >>> len(collect("@@notes.md @@notes.md", set())[2])
    1
    >>> def run(cmd):
    ...     return (0, b"out\n") if cmd.startswith("ok") else (1, b"")
    >>> clean, header, adds = collect("%%ok", set(), run)
    >>> clean
    '%%ok'
    >>> header
    'ok\n---\nout\n\n---\n'
    >>> [(a["origin"], a["key"], a["kind"]) for a in adds]
    [('cmd', 'ok', 'text')]
    >>> adds[0]["hash"] == hashlib.sha256(b"out\n").hexdigest()
    True
    >>> h = hashlib.sha256(b"out\n").hexdigest()
    >>> collect("%%ok", {("cmd", "ok", h)}, run)
    ('%%ok', '', [])
    >>> len(collect("%%ok\n%%ok", set(), run)[2])
    1
    >>> collect("%%bad", set(), run)
    Traceback (most recent call last):
        ...
    dic.tty.DicError: %%: bad: exited 1
    >>> collect("%%ok", set())
    Traceback (most recent call last):
        ...
    dic.tty.DicError: %%: ok: no executor to run it
    >>> _ = open("blob.bin", "wb").write(b"\x00\x01\x02")
    >>> header, adds = collect("@@blob.bin", set())[1:]
    >>> header
    ''
    >>> [(a["path"], a["kind"]) for a in adds]
    [('blob.bin', 'binary')]
    >>> _ = open("weird.md", "w").write("@@notes.md\n")
    >>> collect("@@weird.md", set())[1]
    'weird.md\n---\n@@notes.md\n\n---\n'
    >>> _ = open("cmd.md", "w").write("%%bad\n")
    >>> collect("@@cmd.md", set(), run)[1]
    'cmd.md\n---\n%%bad\n\n---\n'
    >>> collect("@@nope.md", set())
    Traceback (most recent call last):
        ...
    dic.tty.DicError: nope.md: no such file; write \@@nope.md for a literal
    >>> os.mkdir("sub")
    >>> collect("@@sub", set())
    Traceback (most recent call last):
        ...
    dic.tty.DicError: sub: is a directory; use $(accio sub/) instead
    >>> os.symlink("gone", "dead")
    >>> collect("@@dead", set())
    Traceback (most recent call last):
        ...
    dic.tty.DicError: dead: dangling symlink
    >>> collect("@@https://example.com/x", set())
    Traceback (most recent call last):
        ...
    dic.tty.DicError: https://example.com/x: refusing to fetch a URL; @@ names a local file
    >>> os.chdir(old); shutil.rmtree(d)
    """
    from dic.tty import DicError  # lazy: a prompt with no mention pays nothing

    clean, spans = scan(text)
    blocks = []
    adds = []
    seen = set()
    for item in known:
        item = tuple(item)
        seen.add(("file", *item) if len(item) == 2 else item)
    for _start, _end, origin, key in spans:
        if origin == "cmd":
            if run is None:
                raise DicError(f"%%: {key}: no executor to run it")
            rc, out = run(key)
            if rc != 0:
                raise DicError(f"%%: {key}: exited {rc}")
            digest = hashlib.sha256(out).hexdigest()
            if ("cmd", key, digest) in seen:
                continue
            seen.add(("cmd", key, digest))
            blocks.append(
                f"{key}\n---\n{out.decode('utf-8', 'replace')}\n---\n")
            adds.append({"origin": "cmd", "key": key, "hash": digest,
                         "kind": "text"})
            continue
        path = key
        if "://" in path:
            raise DicError(
                f"{path}: refusing to fetch a URL; @@ names a local file")
        if not os.path.exists(path):
            if os.path.islink(path):
                raise DicError(f"{path}: dangling symlink")
            raise DicError(
                f"{path}: no such file; write \\@@{path} for a literal")
        if os.path.isdir(path):
            raise DicError(
                f"{path}: is a directory; use $(accio {path}/) instead")
        if not os.path.isfile(path):
            raise DicError(f"{path}: not a regular file")
        with open(path, "rb") as f:
            data = f.read()
        digest = hashlib.sha256(data).hexdigest()
        if ("file", path, digest) in seen:
            continue
        seen.add(("file", path, digest))
        if b"\x00" in data[:SNIFF]:
            adds.append({"path": path, "hash": digest, "kind": "binary"})
            continue
        body = data.decode("utf-8", "replace")
        blocks.append(f"{path}\n---\n{body}\n---\n")
        adds.append({"path": path, "hash": digest, "kind": "text"})
    return clean, "".join(blocks), adds