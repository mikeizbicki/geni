"""Gathering: the files, the context above them, and the prompt they make.

`accio` walks the paths it is given, skips what cannot be a prompt, and
prints each file under its own path so that whoever reads the result can
cite where a line came from.  The one thing it does that `cat` cannot is
the ancestor walk: before a file is printed, the README and AGENTS files
of every directory above it are printed first, once, so that a subtree's
conventions arrive before the code that obeys them.

Nothing here reads the network or calls a model, so importing it costs
What a walk skips is decided by the repository the path is in: git is
asked which files it ignores rather than being second-guessed, so nested
`.gitignore` files, `!` negation and `/` anchoring are git's rules and
not a matcher here that gets them subtly wrong.  Nothing here reads the
network or calls a model, so importing it costs one file of pure stdlib.
See SPEC.md.

    accio src/                every file under src, plus its READMEs
    accio a.py b.py           two files, plus the READMEs above them
"""
import fnmatch, os, subprocess

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

# Where a path sits and what its repository ignores: one answer per
# directory and one per repository, because both are facts about a whole
# tree and neither should cost a subprocess for every file in it.
_ROOTS = {}
_ALLOWED = {}


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


def gitroot(directory):
    """The toplevel of the git repository that contains a directory.

    Git marks a working tree with a `.git` entry -- a directory in an
    ordinary clone, a file in a submodule or a linked worktree -- so the
    search is for that one name in each directory upward.  Stopping at the
    first one found is what makes a submodule its own root, and therefore
    what makes the enclosing repository's rules stop at it.

    >>> import tempfile, shutil
    >>> d = tempfile.mkdtemp(); os.makedirs(d + "/a/b"); os.mkdir(d + "/.git")
    >>> gitroot(d + "/a/b") == d
    True
    >>> shutil.rmtree(d)
    """
    directory = os.path.abspath(directory)
    if directory not in _ROOTS:
        _ROOTS[directory] = _search(directory)
    return _ROOTS[directory]


def _search(directory):
    """The nearest `.git` above a directory, or None at the filesystem root."""
    while True:
        if os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def gitallow(root):
    """What git does not ignore inside a repository, or None if it cannot say.

    One subprocess answers for the whole tree.  Nested `.gitignore` files,
    `!` negation and `/` anchoring are git's rules and are not
    reimplemented here, because a matcher that gets them subtly wrong is
    one that puts a file into a prompt that nobody meant to send.
    `ls-files` also stops at a submodule, listing the gitlink and nothing
    beneath it, so a submodule is skipped by the same command that skips
    an ignored file.  A repository git cannot read is no repository, which
    is also what a machine without git gets: the default patterns, and
    nothing else.

    >>> gitallow(None) is None
    True
    """
    if root is None:
        return None
    if root not in _ALLOWED:
        listing = None
        try:
            done = subprocess.run(  # noqa: S603
                ["git", "-C", root, "ls-files", "-co",  # noqa: S607
                 "--exclude-standard", "-z"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            done = None
        if done is not None and done.returncode == 0:
            listing = done.stdout.decode("utf-8", "surrogateescape")
        _ALLOWED[root] = None if listing is None else set(listing.split("\0"))
    return _ALLOWED[root]


def kept(path, patterns=DEFAULT_IGNORES, gitignore=True):
    """Whether a path a walk found, rather than one named outright, survives.

    The repository asked about a path is the one that contains that path
    and not the one that contains the working directory, so a walk that
    steps outside its tree -- `accio ../other/` -- is governed by whatever
    is above where it lands, and a path with no repository above it is
    filtered by the default patterns alone.

    >>> import tempfile, shutil
    >>> d = tempfile.mkdtemp()
    >>> kept(d + "/x.py"), kept(d + "/x.pyc")
    (True, False)
    >>> shutil.rmtree(d)
    """
    if ignored(os.path.basename(path), patterns):
        return False
    if not gitignore:
        return True
    root = gitroot(os.path.dirname(path) or ".")
    allowed = gitallow(root)
    return allowed is None or os.path.relpath(path, root) in allowed


def walk(top, patterns=DEFAULT_IGNORES, gitignore=True):
    """Every file under one directory, sorted at each level, depth-first.

    Files come before subdirectories at each level and each level is
    sorted, so the same tree prints the same bytes in the same order and
    the output is still a prefix `dic --cache` may already have paid for.
    A directory that is itself a repository -- a submodule, or a checkout
    someone nested inside a checkout -- is not entered, because it is a
    different tree with different rules.  Naming it is how the caller asks
    for it, and the walk that starts there starts at its own root and
    obeys its own ignores.

    An unreadable directory holds nothing, and a link to a directory is
    walked past rather than followed.
    """
    try:
        entries = sorted(os.listdir(top))
    except OSError:
        return
    dirs, names = [], []
    for name in entries:
        path = os.path.join(top, name)
        (dirs if os.path.isdir(path) else names).append(path)
    for path in names:
        if kept(path, patterns, gitignore):
            yield path
    for path in dirs:
        if ignored(os.path.basename(path), patterns) or os.path.islink(path):
            continue
        if gitignore and os.path.exists(os.path.join(path, ".git")):
            continue
        yield from walk(path, patterns, gitignore)


def files(paths, patterns=DEFAULT_IGNORES, gitignore=True):
    """Every file the given paths name, in a stable order.

    A directory is walked and a file is summoned as itself, ignoring and
    all: the rules are about what a walk stumbles into, and not about what
    the caller asked for by name.  Naming a directory therefore does not
    open the files inside it that git closed; naming a file does.

    >>> files([]), files([], ())
    ([], [])
    """
    out = []
    for path in paths:
        if os.path.isfile(path):
            out.append(path)
        else:
            out.extend(walk(path, patterns, gitignore))
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


def bundle(paths, readmes=DEFAULT_READMES, patterns=DEFAULT_IGNORES,
           gitignore=True):
    """Yields (path, text) for all that is to be printed, context once.

    A context file is printed the first time some file needs it and never
    again, so the root README arrives with the first file beneath it and
    the thousandth file of the tree does not repeat it.  Context is
    filtered like everything else, so an ignored README is not a way to
    smuggle back a file the walk just refused.

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
    seen = set()
    for path in files(paths, patterns, gitignore):
        for context in ancestors(path, readmes):
            key = os.path.normpath(context)
            if key in seen or not kept(context, patterns, gitignore):
                continue
            seen.add(key)
            text = read(context)
            if text is not None:
                yield context, text
        key = os.path.normpath(path)
        if key in seen:
            continue
        seen.add(key)
        text = read(path)
        if text is not None:
            yield path, text


def bundle_paths(paths, readmes=DEFAULT_READMES, patterns=DEFAULT_IGNORES,
                 gitignore=True):
    """The paths bundle() would emit, in the order it would emit them.

    Same traversal, same ancestor walk, same dedup -- a projection of
    bundle() and not a second implementation of it.

    >>> import tempfile
    >>> d = tempfile.mkdtemp()
    >>> os.makedirs(d + "/a")
    >>> for name, text in (("README.md", "root\\n"), ("a/README.md", "sub\\n"),
    ...                    ("a/x.py", "x = 1\\n"), ("a/y.py", "y = 2\\n")):
    ...     _ = open(os.path.join(d, name), "w").write(text)
    >>> [os.path.relpath(p, d) for p in bundle_paths([d + "/a"])
    ...  if p.startswith(d)]
    ['README.md', 'a/README.md', 'a/x.py', 'a/y.py']
    >>> import shutil; shutil.rmtree(d)
    """
    for path, _ in bundle(paths, readmes, patterns, gitignore):
        yield path


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
