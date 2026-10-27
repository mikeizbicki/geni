"""Include directives for dic prompts.

A prompt may name files with @@path. dic hoists each named file into a
header block at the head of the request and leaves the prose alone. This
module owns the scanner: finding the mentions and consuming the \@@ escape
that spells a literal @@.
"""

import hashlib, os

from accio.traverse import SNIFF


def scan(text):
    r"""Find @@path mentions in *text*.

    Returns ``(clean, spans)``. ``clean`` is *text* with every ``\@@``
    rewritten to ``@@``, since the backslash is dic's escape and must not
    reach the model. ``spans`` is an ordered list of ``(start, end, path)``
    for each mention found in ``clean``, in first-occurrence order; a file
    named twice is reported twice, because the caller decides what to do
    about the duplicate.

    A mention begins at a ``@@`` at the start of the string or just after a
    whitespace character, and its path runs to the next whitespace. No
    punctuation is trimmed, so ``see @@notes.md.`` names ``notes.md.``. A
    bare ``@@`` at end of input, or one followed by whitespace, is not a
    mention.

    >>> scan("@@a.py")
    ('@@a.py', [(0, 6, 'a.py')])
    >>> scan("see @@a.py")
    ('see @@a.py', [(4, 10, 'a.py')])
    >>> scan("@@a @@b")
    ('@@a @@b', [(0, 3, 'a'), (4, 7, 'b')])
    >>> scan("\\@@a")
    ('@@a', [])
    >>> scan("@@")
    ('@@', [])
    >>> scan("see @@notes.md.")
    ('see @@notes.md.', [(4, 15, 'notes.md.')])
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
        if text.startswith("@@", i) and (i == 0 or text[i - 1].isspace()):
            j = i + 2
            k = j
            while k < n and not text[k].isspace():
                k += 1
            if k > j:
                clean.append(text[i:k])
                spans.append((pos, pos + k - i, text[j:k]))
                pos += k - i
                i = k
                continue
        clean.append(text[i])
        pos += 1
        i += 1
    return "".join(clean), spans


def collect(text, known=()):
    r"""Hoist every @@path mention in *text* into a header block.

    Returns ``(clean, header, adds)``. ``clean`` is *text* with the
    ``\@@`` escape consumed and nothing else changed. ``header`` is the
    concatenation, in first-mention order, of one block per newly included
    text file, each rendered as ``path\n---\ntext\n---\n`` -- the
    files-to-prompt encoding the rest of accio already uses. ``adds`` is a
    list of ``{"path", "hash", "kind"}`` records for every file this call
    added, so the caller can store them and pass them back as *known* on a
    later turn.

    *known* is a collection of ``(path, sha256)`` pairs an ancestor turn
    already carried. A mention whose file hashes to one of those
    contributes neither a block nor an add, and a file named twice in one
    call is likewise added once. The header is never rescanned: a ``@@``
    inside an included file's own bytes is literal text.

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
    >>> _ = open("blob.bin", "wb").write(b"\x00\x01\x02")
    >>> header, adds = collect("@@blob.bin", set())[1:]
    >>> header
    ''
    >>> [(a["path"], a["kind"]) for a in adds]
    [('blob.bin', 'binary')]
    >>> _ = open("weird.md", "w").write("@@notes.md\n")
    >>> collect("@@weird.md", set())[1]
    'weird.md\n---\n@@notes.md\n\n---\n'
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
    from dic.tty import DicError  # lazy: a prompt with no @@ pays nothing

    clean, spans = scan(text)
    blocks = []
    adds = []
    seen = set(known)
    for _start, _end, path in spans:
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
        if (path, digest) in seen:
            continue
        seen.add((path, digest))
        if b"\x00" in data[:SNIFF]:
            adds.append({"path": path, "hash": digest, "kind": "binary"})
            continue
        body = data.decode("utf-8", "replace")
        blocks.append(f"{path}\n---\n{body}\n---\n")
        adds.append({"path": path, "hash": digest, "kind": "text"})
    return clean, "".join(blocks), adds