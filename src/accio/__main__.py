#!/usr/bin/env python3
"""accio - summon the contents of files into one prompt.  See SPEC.md.

The whole CLI: turn argv into one traversal, select from it, and render the
result to stdout.

    accio/traverse.py   the walk, the ancestor context, the chunks
    accio/select.py     which of them survive -- all, or a rag ranking
    accio/render.py     the one format, and its projections
    accio/index.py      the sqlite file the ranking reads
    accio/SPEC.md       what the program is for
"""
import argparse, os, sys

from accio import index
from accio.traverse import DEFAULT_IGNORES, DEFAULT_READMES, traverse
from accio.select import select, rag
from accio.render import FORMATS, budget, elide, parse_target


def parser():
    """The argument parser: the paths, the context names, the ignores."""
    p = argparse.ArgumentParser(
        prog="accio", description="summon files into one prompt on stdout")
    p.add_argument("path", nargs="+", metavar="PATH",
                   help="a file or directory to summon")
    p.add_argument("-r", "--readme", action="append", default=None,
                   metavar="NAME",
                   help="summon this file from every directory above a"
                        " summoned file; repeatable"
                        f" (default: {' '.join(DEFAULT_READMES)})")
    p.add_argument("--no-readme", action="store_true",
                   help="summon no file above the ones named")
    p.add_argument("-i", "--ignore", action="append", default=None,
                   metavar="PATTERN",
                   help="walk past a name matching this shell pattern;"
                        " repeatable, on top of the defaults"
                        f" ({' '.join(DEFAULT_IGNORES)})")
    p.add_argument("--no-gitignore", action="store_true",
                   help="do not consult git: summon what .gitignore"
                        " excludes, and walk into submodules")
    p.add_argument("--rag", metavar="QUERY", default=None,
                   help="rank the selection by relevance to QUERY"
                        " (bm25; free)")
    p.add_argument("--refresh", action="store_true",
                   help="rebuild the index from the walk; free")
    p.add_argument("--prune", action="store_true",
                   help="delete the index and exit")
    p.add_argument("--explain", action="store_true",
                   help="print what would be indexed and what is")
    p.add_argument("--index-dir", metavar="DIR", default=None,
                   help="where the index lives; $ACCIO_INDEX_DIR is"
                        " the default")
    p.add_argument("--target", metavar="SIZE[UNIT]", default=None,
                   help="truncate the rendered prompt to about SIZE;"
                        " bytes by default, k and m for kibibytes and"
                        " mebibytes, and T, W, L, C for tokens, words,"
                        " lines and chunks")
    p.add_argument("--fudge", metavar="F", type=float, default=0.25,
                   help="how much the last chunk may overshoot"
                        " --target (default: 1/4)")
    p.add_argument("--elide", action="append", metavar="KIND", default=None,
                   help="drop the named part of each chunk and keep the"
                        " node; `kind=body` drops the body of a Python"
                        " def or class; repeatable")
    p.add_argument("--format", choices=tuple(FORMATS), default=None,
                   metavar="FMT",
                   help="how to encode the selection:"
                        " " + ", ".join(FORMATS))
    p.add_argument("-l", "--paths-only", action="store_true",
                   help="print only the paths that would be gathered,"
                        " and no contents; same as --format paths")
    p.add_argument("-w", "--wc", action="store_true",
                   help="print line, word and byte counts per path and an"
                        " estimated token count for the whole prompt,"
                        " and no contents; same as --format wc")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    missing = [path for path in args.path if not os.path.exists(path)]
    if missing:
        sys.stderr.write(f"accio: no such path: {', '.join(missing)}\n")
        return 1
    readmes = () if args.no_readme else tuple(
        DEFAULT_READMES if args.readme is None else args.readme)
    patterns = DEFAULT_IGNORES + tuple(args.ignore or ())
    gitignore = not args.no_gitignore
    try:
        return run(args, readmes, patterns, gitignore)
    except BrokenPipeError:
        # `accio src/ | head` closes the pipe early, which is the reader's
        # privilege and not a failure to report; stdout is pointed at
        # /dev/null as well, so the interpreter's own flush at exit is quiet
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


def run(args, readmes, patterns, gitignore):
    """Dispatch: the index verbs first, then the walk or the rag ranking."""
    if args.prune:
        index.prune(args.path, args.index_dir)
        return 0
    if args.explain:
        index.explain(args.path, readmes, patterns, gitignore,
                      args.index_dir, sys.stdout)
        sys.stdout.flush()
        return 0
    if args.refresh and args.rag is None:
        conn = index.open_(args.path, args.index_dir)
        index.refresh(conn, args.path, readmes, patterns, gitignore,
                      force=True)
        return 0
    size = None
    if args.target is not None:
        try:
            size = parse_target(args.target)
        except ValueError:
            sys.stderr.write(
                f"accio: --target: not a size: {args.target}\n")
            return 1
    if args.rag is not None:
        chunks = rag(args.rag, args.path, readmes, patterns, gitignore,
                     args.index_dir, force=args.refresh)
    else:
        chunks = select(traverse(args.path, readmes, patterns, gitignore))
    for what in [a.split("=")[-1] for a in (args.elide or ())]:
        chunks = elide(chunks, what)
    if size is not None:
        chunks = budget(chunks, size[0], size[1], args.fudge)
    fmt = args.format or ("paths" if args.paths_only
                          else "wc" if args.wc else "prompt")
    FORMATS[fmt](chunks, sys.stdout)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
