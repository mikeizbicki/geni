"""Gathering: the files, the context above them, and the prompt they make.

`accio` walks the paths it is given, skips what cannot be a prompt, and
prints each file under its own path so that whoever reads the result can
cite where a line came from.  The one thing it does that `cat` cannot is
the ancestor walk: before a file is printed, the README and AGENTS files
of every directory above it are printed first, once, so that a subtree's
conventions arrive before the code that obeys them.

Nothing here reads the network or calls a model, so importing it costs
one file of pure stdlib.  See SPEC.md.

    accio src/                every file under src, plus its READMEs
    accio a.py b.py           two files, plus the READMEs above them
"""
import fnmatch, os

# The names looked for in every directory above a summoned file, in the
# order they are tried.  A README states what a subtree is and an AGENTS
# file states how to change it; both are context the model needs *before*
# the code they explain, which is why they are printed above it.
DEFAULT_READMES = ("README", "README.md", "AGENTS.md")

# What a walk never stumbles into: version control state, bytecode caches,
# and the directories a package manager fills.  A binary *file* is caught
# by its bytes and not by a name, so this list is only about what is never
# a prompt whatever it holds.
DEFAULT_IGNORES = (".git", "__pycache__", ".mypy_cache", ".pytest_cache",
                   ".venv", "venv", "node_modules", "*.pyc", "*.pyo",
                   "*.egg-info", ".DS_Store")

# How much of a file is sniffed for the NUL byte that says it is not text.
SNIFF = 8192


def ignored(name, patterns=DEFAULT_IGNORES):
    """Whether one path component matches any ignore pattern.

    Patterns are shell globs, matched against the component and not
    against the whole path, so `node_modules` refuses that directory
    wherever it appears and `*.pyc` refuses that file inside it.

    >>> ignored(".git"), ignored("client.py"), ignored("x.pyc")
    (True, False, True)
    """
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def read(path):
    """The text of a file, or None when it is not text at all.

    Binary is decided by a NUL byte in the first block, which is the test
    `git`, `grep(1)` and `file(1)` all begin with and which needs no table
    of extensions, because a text file of any language has none.  A file
    that is skipped is skipped in silence: a warning about a file the
    caller never named is noise, and `accio` prints the prompt and nothing
    else.

    Decoding is utf-8 with bad bytes replaced rather than fatal, because a
    file that is 99% text is still a file the model should be given, and
    one byte it cannot read is one character and not the end of the prompt.

    >>> "def read(path)" in (read(os.path.abspath(__file__)) or "")
    True
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\x00" in data[:SNIFF]:
        return None
    return data.decode("utf-8", "replace")


def files(paths, patterns=DEFAULT_IGNORES):
    """Every file the given paths name, in a stable order.

    A directory is walked with its entries sorted at each level, so the
    same tree prints the same bytes in the same order, and a prompt built
    from it can be cached, diffed and compared.  A file named outright is
    taken whatever it is called: ignoring is about what a walk stumbles
    into, and not about what the caller asked for.

    >>> files([]), files([], ())
    ([], [])
    """
    out = []
    for path in paths:
        if os.path.isfile(path):
            out.append(path)
            continue
        for root, dirs, names in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not ignored(d, patterns))
            out.extend(os.path.join(root, name) for name in sorted(names)
                       if not ignored(name, patterns))
    return out


def ancestor_dirs(path):
    """The directories above a file, outermost first, its own one last.

    The walk stops at the filesystem root, so an absolute path means
    context from `/` down and a relative one means context from `.` down.

    >>> ancestor_dirs("a/b/c.py")
    ['.', 'a', 'a/b']
    >>> ancestor_dirs("c.py")
    ['.']
    >>> ancestor_dirs("/x/y/c.py")
    ['/', '/x', '/x/y']
    """
    directory = os.path.normpath(os.path.dirname(path) or ".")
    seen = []
    while True:
        seen.append(directory)
        parent = os.path.dirname(directory)
        if parent == directory:       # "/" is its own parent: the root
            break
        if parent:
            directory = parent
        elif directory != ".":        # a relative top: the walk ends at "."
            directory = "."
        else:
            break
    return list(reversed(seen))


def ancestors(path, readmes=DEFAULT_READMES):
    """The context files above one file, outermost first.

    For `a/b/c.py` these are the ones found in `.`, `a` and `a/b`, in that
    order, so a convention stated at the root is read before the one that
    refines it and the file's own directory speaks immediately before it.
    Every name that is found in a directory is taken, so a tree keeping
    both a README and an AGENTS file loses neither.

    >>> import tempfile
    >>> d = tempfile.mkdtemp()
    >>> os.makedirs(d + "/a")
    >>> for name in ("README.md", "a/AGENTS.md"):
    ...     open(os.path.join(d, name), "w").close()
    >>> [os.path.relpath(p, d) for p in ancestors(d + "/a/b.py")
    ...  if p.startswith(d)]
    ['README.md', 'a/AGENTS.md']
    >>> import shutil; shutil.rmtree(d)
    """
    out = []
    for directory in ancestor_dirs(path):
        for name in readmes:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                out.append(candidate)
    return out


def bundle(paths, readmes=DEFAULT_READMES, patterns=DEFAULT_IGNORES):
    """(path, text) pairs for all that is to be printed, context included once.

    A context file is printed the first time some file needs it and never
    again, so the root README arrives with the first file beneath it and
    the thousandth file of the tree does not repeat it.

    >>> import tempfile
    >>> d = tempfile.mkdtemp()
    >>> os.makedirs(d + "/a")
    >>> for name, text in (("README.md", "root\\n"), ("a/README.md", "sub\\n"),
    ...                    ("a/x.py", "x = 1\\n"), ("a/y.py", "y = 2\\n")):
    ...     _ = open(os.path.join(d, name), "w").write(text)
    >>> [(os.path.relpath(p, d), t) for p, t in bundle([d + "/a"])
    ...  if p.startswith(d)]
    [('README.md', 'root\\n'), ('a/README.md', 'sub\\n'), ('a/x.py', 'x = 1\\n'), ('a/y.py', 'y = 2\\n')]
    >>> # the same path named twice is still one entry:
    >>> [(os.path.relpath(p, d), t)
    ...  for p, t in bundle([d + "/a", d + "/README.md"])
    ...  if p.startswith(d)]
    [('README.md', 'root\\n'), ('a/README.md', 'sub\\n'), ('a/x.py', 'x = 1\\n'), ('a/y.py', 'y = 2\\n')]
    >>> import shutil; shutil.rmtree(d)
    """
    seen, out = set(), []
    for path in files(paths, patterns):
        for context in ancestors(path, readmes):
            key = os.path.normpath(context)
            if key not in seen:
                seen.add(key)
                text = read(context)
                if text is not None:
                    out.append((context, text))
        key = os.path.normpath(path)
        if key in seen:
            continue
        seen.add(key)
        text = read(path)
        if text is not None:
            out.append((path, text))
    return out


def render(pairs, out):
    """Write (path, text) pairs as the one format accio has: path, fences, text.

    The path is on a line of its own, a `---` fence follows it, the text
    follows that, and a closing `---` follows the text, so that a reader
    -- a model, a human, `less` -- can see where one file stops, and so
    that the output is the encoding files-to-prompt already produces.  A
    fence closes every file, so the last line of the whole run is always
    `---`.  The newline below the text is written and never assumed: a
    file whose last line has no newline of its own must not run into the
    closing fence.

    >>> import io
    >>> stream = io.StringIO()
    >>> render([("a.py", "x = 1\\n")], stream)
    >>> stream.getvalue()
    'a.py\\n---\\nx = 1\\n---\\n'
    >>> stream = io.StringIO()
    >>> render([("b.txt", "no newline")], stream)
    >>> stream.getvalue()
    'b.txt\\n---\\nno newline\\n---\\n'
    """
    for path, text in pairs:
        out.write(f"{path}\n---\n{text}")
        if not text.endswith("\n"):
            out.write("\n")
        out.write("---\n")
