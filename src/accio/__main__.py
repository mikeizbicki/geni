#!/usr/bin/env python3
"""accio - summon the contents of files into one prompt.  See SPEC.md.

The whole CLI: turn argv into one bundle() call and write it to stdout.

    accio/gather.py     the walk, the ancestor context, the format
    accio/SPEC.md       what the program is for
"""
import argparse, os, sys

from accio.gather import (DEFAULT_IGNORES, DEFAULT_READMES, bundle,
                          bundle_paths, render, wc)


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
    p.add_argument("-l", "--paths-only", action="store_true",
                   help="print only the paths that would be gathered,"
                        " one per line, and no contents")
    p.add_argument("-w", "--wc", action="store_true",
                   help="print line, word and byte counts per path and an"
                        " estimated token count for the whole prompt,"
                        " and no contents")
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
    try:
        if args.paths_only:
            for path in bundle_paths(args.path, readmes, patterns,
                                     not args.no_gitignore):
                sys.stdout.write(f"{path}\n")
        elif args.wc:
            wc(bundle(args.path, readmes, patterns, not args.no_gitignore),
               sys.stdout)
        else:
            render(bundle(args.path, readmes, patterns, not args.no_gitignore),
                   sys.stdout)
        sys.stdout.flush()
    except BrokenPipeError:
        # `accio src/ | head` closes the pipe early, which is the reader's
        # privilege and not a failure to report; stdout is pointed at
        # /dev/null as well, so the interpreter's own flush at exit is quiet
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
