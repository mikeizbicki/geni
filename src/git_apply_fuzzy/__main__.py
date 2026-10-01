#!/usr/bin/env python3
"""git-apply-fuzzy - apply a unified diff with fuzzy matching.

A drop-in replacement for `git apply` for when the model wrote context
lines that are *almost* right.  `git apply` needs the whole hunk to match
exactly, so one paraphrased word anywhere in the context kills it; this
tries, in order:

    1. an exact match,
    2. a match ignoring trailing whitespace,
    3. a difflib similarity match at or above --threshold (default 0.8).

Level 3 survives a drifted context line, and it is the only level that
can be *wrong*: at 0.8, one line in five may differ.  The window is the
one nearest the hunk's own line number, which is what a human reading
the diff would pick.

When the match is fuzzy, only the lines the hunk actually changed are
rewritten -- each context line keeps the file's own version of itself,
so a fuzzy match cannot "correct" a line of the file into the model's
guess at it.

A patch says more than where lines go, and the rest of it is read too: a
rename is a rename and not a delete beside a create, a mode change is a
chmod, and a symlink is a symlink.  Spelling a move out as content is what
it costs otherwise, a deleted line for every line of the file.

The repository is not touched until every hunk has been located, so a
patch that fails to match somewhere leaves it exactly as it was.
`--force` opts back into writing the hunks that did apply.

    fuzzy-apply                     # .git/committe-patchfile
    fuzzy-apply --dry-run -v
    fuzzy-apply -t 0.9 patch.diff
"""
import argparse, difflib, os, re, subprocess, sys

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_RE = re.compile(r"^diff --git ")

# A `diff --git` line whose two names are equal, which is the only place the
# path of a mode-only change or of a delete whose preimage was left out
# appears: Saying the two names are equal with a backreference is what splits
# the line without having to guess where a path with a space in it ended.
HEADER_RE = re.compile(r"^diff --git a/(?P<path>.*) b/(?P=path)$")

# The two modes whose "content" is not the bytes of a file: a symlink's is
# the path it points at and a gitlink's is the commit it names, so neither
# can be written with open() and neither is a hunk over real text.
SYMLINK, GITLINK = "120000", "160000"

# Set by --quiet; note() respects it, error() does not.
QUIET = False


def note(message):
    """One informational line on stderr; --quiet silences it."""
    if not QUIET:
        sys.stderr.write(message + "\n")


def error(message):
    """An error line on stderr; --quiet never silences it."""
    sys.stderr.write(message + "\n")


def stage(path):
    """Stage path with `git add`, the way `git apply --index` does.

    Run after the write, so a failure here only means the user has to
    stage the file by hand; the file on disk is already correct.
    """
    result = subprocess.run(["git", "add", "-A", "--", path],
                            capture_output=True, text=True)
    if result.returncode != 0:
        error(f"{path}: git add failed: {result.stderr.strip()}")
        return False
    return True


def _path(text):
    """A ---/+++ path, or None for /dev/null."""
    text = text.strip()
    return None if text == "/dev/null" else text


def strip_path(path, n):
    """Remove n leading path components, as `git apply -p` does.

    >>> strip_path("a/dic/tty.py", 1), strip_path("a/b/c", 2)
    ('dic/tty.py', 'c')
    """
    return "/".join(path.split("/")[n:]) if path else path


def unprefixed(path):
    """A rename header's path, without the a/ or b/ a hand-written patch adds.

    Git states the two names of a rename without the a/ and b/ prefixes that
    the `diff --git` line above them carries, so there is normally nothing to
    remove; a patch written by hand tends to copy them anyway, and either is
    accepted here.

    >>> unprefixed("x.txt"), unprefixed("a/x.txt"), unprefixed("b/x.txt")
    ('x.txt', 'x.txt', 'x.txt')
    """
    return path[2:] if path[:2] in ("a/", "b/") else path


def header_path(line):
    """The path of a `diff --git` line whose two names are the same, or None.

    A patch that only changes a mode, and a delete whose preimage git was
    told to leave out, carry no `---` or `+++` line, so the `diff --git`
    line is the only place their path appears.

    >>> header_path("diff --git a/x b/x\\n")
    'a/x'
    >>> header_path("diff --git a/with space b/with space\\n")
    'a/with space'
    >>> header_path("diff --git a/x b/y\\n") is None
    True
    """
    match = HEADER_RE.match(line)
    return "a/" + match["path"] if match else None


def permissions(mode):
    """The chmod bits of a git mode, which also names the file's type.

    '100644' is the octal file type 100, a regular file, beside the
    permission bits 644, so chmod is given the low ones and not the whole
    number.

    >>> permissions("100644") == 0o644, permissions("100755") == 0o755
    (True, True)
    >>> permissions("160000")           # a submodule: no permissions at all
    0
    """
    return int(mode, 8) & 0o7777


def chmod(path, mode):
    """Give path a git mode's permission bits, and a symlink none.

    A file written from a patch is created with the umask's permissions, so
    this is what applies a `new file mode 100755` and an `old mode`/`new
    mode` pair.  A symlink's permissions are not its own and a gitlink is
    not a file at all, so both are left alone.
    """
    if mode and mode not in (SYMLINK, GITLINK):
        os.chmod(path, permissions(mode))


def body_of(hunks):
    """The added lines of a file's hunks: the whole of a file being created.

    >>> body_of([(1, [('-', 'a\\n'), ('+', 'b\\n'), ('+', 'c\\n')])])
    ['b\\n', 'c\\n']
    >>> body_of([])
    []
    """
    return [line for _, hunk in hunks for tag, line in hunk if tag == "+"]


def gitlink_sha(hunks):
    """The commit a submodule's hunk names.

    Git writes a gitlink's content as the one line `Subproject commit <oid>`,
    so the oid is the last word of the line the hunk adds.

    >>> gitlink_sha([(1, [('-', 'Subproject commit aaa\\n'),
    ...                   ('+', 'Subproject commit bbb\\n')])])
    'bbb'
    >>> gitlink_sha([])
    ''
    """
    return "".join(body_of(hunks)).rstrip("\n").rsplit(" ", 1)[-1]


def submodule_argv(path, commit):
    """The git command that records a submodule at a commit, or removes one.

    A gitlink is an index entry and nothing else: its content is a commit id
    and the checkout below the path belongs to git, so it is the one change
    a patch can ask for that is made by asking git rather than by writing.

    >>> submodule_argv("s", "abc")
    ['git', 'update-index', '--add', '--cacheinfo', '160000,abc,s']
    >>> submodule_argv("s", None)
    ['git', 'update-index', '--force-remove', '--', 's']
    """
    if commit:
        return ["git", "update-index", "--add",
                "--cacheinfo", f"{GITLINK},{commit},{path}"]
    return ["git", "update-index", "--force-remove", "--", path]


def set_submodule(path, commit):
    """Record or drop the index entry of a submodule, and say whether it worked.

    The submodule's own working tree is left as it is: moving a checkout to
    the commit the patch names is `git submodule update`'s job, and it needs
    a network this program does not have.
    """
    result = subprocess.run(submodule_argv(path, commit),
                            capture_output=True, text=True)
    if result.returncode != 0:
        note(f"{path}: {result.stderr.strip()}")
        return False
    return True


def parse(text):
    """A unified diff as a list of {old, new, hunks}.

    Anything before the first `diff --git` (committe's commit message) is
    ignored, exactly as `git apply` ignores it.  A hunk is its old-file
    start line and a list of (tag, line) pairs, the line keeping its
    trailing newline and the tag being ' ', '-' or '+'.

    Git's own header lines are read as well as its hunks, because a patch
    that only renames a path, only changes a mode, or only deletes a file
    has no hunks at all and those lines are then the whole of it.  `rename`
    is the (from, to) pair of a move and None for anything else, and the
    modes are the octal strings git writes, present only where the patch
    changes one.  A change with no `---` or `+++` line -- a mode change, or
    a delete whose preimage was left out -- takes its path from the
    `diff --git` line, where the two names are equal by construction.

    >>> entry = parse("diff --git a/x b/x\\n--- a/x\\n+++ b/x\\n"
    ...               "@@ -1 +1 @@\\n-a\\n+b\\n")[0]
    >>> entry["old"], entry["new"], entry["hunks"]
    ('a/x', 'b/x', [(1, [('-', 'a\\n'), ('+', 'b\\n')])])
    >>> entry["rename"], entry["old_mode"], entry["new_mode"]
    (None, None, None)
    >>> entry = parse("diff --git a/x b/y\\nsimilarity index 100%\\n"
    ...               "rename from x\\nrename to y\\n")[0]
    >>> entry["rename"], entry["old"], entry["hunks"]
    (('x', 'y'), None, [])
    >>> entry = parse("diff --git a/x b/x\\nold mode 100644\\n"
    ...               "new mode 100755\\n")[0]
    >>> entry["old"], entry["new"], entry["old_mode"], entry["new_mode"]
    ('a/x', 'b/x', '100644', '100755')
    """
    files = []
    for chunk in re.split(r"(?m)^(?=diff --git )", text):
        lines = chunk.splitlines(keepends=True)
        if not lines or not DIFF_RE.match(lines[0]):
            continue
        entry = {"old": None, "new": None, "rename": [], "old_mode": None,
                 "new_mode": None, "hunks": []}
        i = 1
        while i < len(lines) and not HUNK_RE.match(lines[i]):
            line = lines[i]
            if line.startswith("--- "):
                entry["old"] = _path(line[4:])
            elif line.startswith("+++ "):
                entry["new"] = _path(line[4:])
            elif line.startswith("rename from "):
                entry["rename"].append(line[12:].rstrip("\n"))
            elif line.startswith("rename to "):
                entry["rename"].append(line[10:].rstrip("\n"))
            elif " mode " in line:
                what, _, mode = line.rstrip("\n").partition(" mode ")
                entry["new_mode" if what.startswith("new") else "old_mode"] = mode
            i += 1
        entry["rename"] = (tuple(entry["rename"])
                           if len(entry["rename"]) == 2 else None)
        if entry["old"] is None and entry["new"] is None and entry["rename"] is None:
            # no `---` and no `+++`: a mode change, or a delete whose
            # preimage git was told to leave out.  Either way the path is on
            # the `diff --git` line, and a delete is the case where only the
            # old mode was stated
            entry["old"] = header_path(lines[0])
            if entry["new_mode"] is not None:
                entry["new"] = entry["old"]
        while i < len(lines):
            match = HUNK_RE.match(lines[i])
            if not match:
                i += 1
                continue
            start = int(match.group(1))
            i += 1
            body = []
            while i < len(lines) and not HUNK_RE.match(lines[i]):
                line = lines[i]
                if line[:1] == "\\":              # \ No newline at end of file
                    i += 1
                    continue
                if line[:1] in (" ", "-", "+"):
                    body.append((line[0], line[1:]))
                elif line == "\n":                # a bare blank context line
                    body.append((" ", "\n"))
                else:
                    break
                i += 1
            entry["hunks"].append((start, body))
        files.append(entry)
    return files


def sides(hunk):
    """The old side (context and '-') and new side (context and '+') of a hunk.

    >>> sides([(' ', 'c\\n'), ('-', 'old\\n'), ('+', 'new\\n')])
    (['c\\n', 'old\\n'], ['c\\n', 'new\\n'])
    """
    return ([line for tag, line in hunk if tag in (" ", "-")],
            [line for tag, line in hunk if tag in (" ", "+")])


def locate(lines, search, hint, threshold):
    """The best window in lines matching search, as (start, ratio).

    Exact first, then ignoring trailing whitespace, then difflib; every
    level prefers the window nearest the hunk's own line number, so a
    block that occurs twice lands where the diff meant it to.  Returns
    (None, 0.0) when nothing reaches the threshold.

    >>> locate(["a\\n", "b\\n", "c\\n"], ["b\\n"], 0, 0.8)
    (1, 1.0)
    >>> locate(["a\\n", "b\\n", "c\\n"], ["b\\n", "x\\n"], 0, 0.8)
    (None, 0.0)
    """
    size = len(search)
    if size == 0:
        return max(0, min(hint, len(lines))), 1.0
    if size > len(lines):
        return None, 0.0
    count = len(lines) - size + 1
    hint = max(0, min(hint, count - 1))
    order = sorted(range(count), key=lambda s: abs(s - hint))
    for start in order:
        if lines[start:start + size] == search:
            return start, 1.0
    bare = [line.rstrip() for line in search]
    for start in order:
        if [line.rstrip() for line in lines[start:start + size]] == bare:
            return start, 1.0
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(search)
    best, best_ratio = None, 0.0
    for start in order:
        matcher.set_seq1(lines[start:start + size])
        if matcher.quick_ratio() < threshold:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio = start, ratio
    return (best, best_ratio) if best_ratio >= threshold else (None, 0.0)


def splice(lines, at, hunk):
    """Apply one hunk's body to lines at position at, keeping the file's context.

    A clean match is a plain slice assignment.  Otherwise the hunk's old
    side and the file's window have the same length by construction, so
    each context line can keep the file's own version of itself and only
    the lines the hunk actually changed are rewritten: a fuzzy match must
    never "correct" a line of the file into the model's guess at it.

    >>> lines = ["ctx\\n", "old\\n", "ctx\\n"]
    >>> splice(lines, 0, [
    ...     (' ', 'ctx\\n'), ('-', 'old\\n'), ('+', 'new\\n'), (' ', 'ctx\\n')])
    >>> lines
    ['ctx\\n', 'new\\n', 'ctx\\n']
    """
    old = [line for tag, line in hunk if tag in (" ", "-")]
    if lines[at:at + len(old)] == old:
        lines[at:at + len(old)] = [line for tag, line in hunk if tag in (" ", "+")]
        return
    result, i = [], 0
    for tag, line in hunk:
        if tag == " ":
            result.append(lines[at + i])
            i += 1
        elif tag == "-":
            i += 1
        else:
            result.append(line)
    lines[at:at + len(old)] = result


def apply_hunks(lines, hunks, threshold):
    """Apply hunks bottom-up; return (lines, matches, failures).

    Bottom-up so that one application does not shift the line numbers the
    next (earlier) hunk is still being located against.  matches is
    (hunk number, line applied at, ratio); failures is (hunk number, the
    line the diff expected to find).

    >>> apply_hunks(["a\\n", "b\\n"], [(1, [('-', 'a\\n'), ('+', 'z\\n')])], 0.8)
    (['z\\n', 'b\\n'], [(1, 1, 1.0)], [])
    >>> apply_hunks(["a\\n"], [(1, [('-', 'q\\n'), ('+', 'z\\n')])], 0.8)
    (['a\\n'], [], [(1, 1)])
    """
    placed, matches, failures = [], [], []
    for index, (start, hunk) in enumerate(hunks, 1):
        old, _ = sides(hunk)
        at, ratio = locate(lines, old, start - 1, threshold)
        if at is None:
            failures.append((index, start))
        else:
            matches.append((index, at + 1, ratio))
            placed.append((at, hunk))
    for at, hunk in sorted(placed, key=lambda p: -p[0]):
        splice(lines, at, hunk)
    return lines, matches, failures


def main():
    global QUIET

    parser = argparse.ArgumentParser(
        prog="fuzzy-apply",
        description="Apply a unified diff, matching context fuzzily.")
    parser.add_argument("patch", nargs="?", default=".git/committe-patchfile",
                        help="the patch to apply (default: %(default)s)")
    parser.add_argument("-t", "--threshold", type=float, default=0.8, metavar="R",
                        help="minimum difflib ratio for a fuzzy match"
                             " (default: %(default)s)")
    parser.add_argument("-p", "--strip", type=int, default=1, metavar="N",
                        help="strip N leading path components (default: %(default)s)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what would happen without writing")
    parser.add_argument("-f", "--force", action="store_true",
                        help="write the hunks that applied even if others did not")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="report every hunk's match ratio")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress informational messages on stderr")
    args = parser.parse_args()

    QUIET = args.quiet

    try:
        with open(args.patch) as handle:
            text = handle.read()
    except OSError as err:
        sys.exit(f"fuzzy-apply: {args.patch}: {err}")

    files = parse(text)
    if not files:
        sys.exit(f"fuzzy-apply: {args.patch}: no diff found")

    failed = False
    created, deleted, written = [], [], []  # planned changes, not yet made
    moved, chmods, submodules = [], [], []  # renames, modes and gitlinks too
    for entry in files:
        hunks = entry["hunks"]
        if entry["rename"] is not None:
            # git states the two names of a rename without the a/ and b/
            # that the `diff --git` line above them carries
            old, new = (unprefixed(entry["rename"][0]),
                        unprefixed(entry["rename"][1]))
        else:
            old = strip_path(entry["old"], args.strip)
            new = strip_path(entry["new"], args.strip)
        path = new or old
        # the mode the path ends up with, or the one a deletion states it
        # had: a gitlink is only tellable from a file by its mode
        mode = entry["new_mode"] or entry["old_mode"]

        if path is None:
            note("fuzzy-apply: an entry names no path to change")
            failed = True
            continue

        if new is None:                     # a deletion: every hunk is a removal
            if mode == GITLINK:
                submodules.append((path, None))
            else:
                deleted.append(path)
            note(f"{path}: deleted")
            continue

        if old is None:                     # a new file: every hunk is an addition
            if mode == GITLINK:
                submodules.append((path, gitlink_sha(hunks)))
                note(f"{path}: submodule -> {gitlink_sha(hunks)[:12]}")
            else:
                body = body_of(hunks)
                created.append((path, body, mode))
                note(f"{path}: created ({len(body)} lines)")
            continue

        if mode == GITLINK:
            # a gitlink's content is a commit id and not bytes, and the
            # checkout below the path is not this program's to make
            submodules.append((path, gitlink_sha(hunks)))
            note(f"{path}: submodule -> {gitlink_sha(hunks)[:12]}")
            continue

        if entry["rename"] is not None:
            moved.append((old, new))
            note(f"{path}: moved from {old}")

        if not hunks:                       # a rename, a mode change, or both
            if mode != entry["old_mode"]:
                chmods.append((path, mode))
                note(f"{path}: mode {entry['old_mode'] or '?'} -> {mode}")
            elif entry["rename"] is None:
                note(f"{path}: no hunks and no mode change")
                failed = True
            continue

        try:
            # a rename is read from where the file still is; anything else
            # from the one path its two sides name
            with open(old if entry["rename"] is not None else path) as handle:
                lines = handle.readlines()
        except OSError as err:
            error(f"{path}: {err}")
            failed = True
            continue

        lines, matches, failures = apply_hunks(lines, hunks, args.threshold)
        if args.verbose:
            for index, line, ratio in matches:
                note(f"  hunk {index}: matched at line {line} (ratio {ratio:.2f})")
        for index, line in failures:
            error(f"  hunk {index}: no match at or above {args.threshold:.2f}"
                  f" near line {line}")
        if failures:
            failed = True
            if not args.force:
                error(f"{path}: {len(failures)}/{len(hunks)} hunks did not match")
                continue
        written.append((path, lines, mode))
        note(f"{path}: {len(matches)}/{len(hunks)} hunks applied")

    # Only now is the tree touched: everything above just read it, so a
    # hunk that does not match anywhere leaves the repository untouched,
    # the way `git apply` does.  `--force` opts back into partial writes.
    if failed and not args.force:
        sys.exit(1)
    if args.dry_run:
        sys.exit(1 if failed else 0)

    applied = []                            # paths written, staged at the end
    for source, destination in moved:
        try:
            os.rename(source, destination)
        except OSError as err:
            error(f"{source}: {err}")
            failed = True
            continue
        applied += [source, destination]
    for path, body, mode in created:
        try:
            if mode == SYMLINK:
                os.symlink("".join(body).rstrip("\n"), path)
            else:
                with open(path, "w") as handle:
                    handle.writelines(body)
                chmod(path, mode)
        except OSError as err:
            error(f"{path}: {err}")
            failed = True
            continue
        applied.append(path)
    for path in deleted:
        try:
            os.remove(path)
        except OSError as err:
            error(f"{path}: {err}")
            failed = True
            continue
        applied.append(path)
    for path, lines, mode in written:
        try:
            if mode == SYMLINK:             # a file the patch turns into a link
                os.remove(path)
                os.symlink("".join(lines).rstrip("\n"), path)
            else:
                with open(path, "w") as handle:
                    handle.writelines(lines)
                chmod(path, mode)
        except OSError as err:
            error(f"{path}: {err}")
            failed = True
            continue
        applied.append(path)
    for path, mode in chmods:
        try:
            os.chmod(path, permissions(mode))
        except OSError as err:
            note(f"{path}: {err}")
            failed = True
            continue
        applied.append(path)

    for path, commit in submodules:
        if not set_submodule(path, commit):
            failed = True

    for path in applied:
        if not stage(path):
            failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
