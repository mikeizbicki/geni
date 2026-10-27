#!/usr/bin/env python3
"""dic-sync -- rebuild the model list from each provider's own catalog.

Reads each provider's `sync` recipe out of the model config, asks the
provider for its catalog through the fetcher the recipe names, and
writes the result to $XDG_STATE_HOME/geni/models.json, which
dic.config.sources() reads below every hand-written source.

    dic-sync          every provider whose API key is exported
    dic-sync -p fal   only fal
    dic-sync -q       nothing but a failure

An install holding one provider's key syncs that one provider's models
and reports the rest skipped, so the command is useful before every
key is set.  See dic_sync/catalog.py for what a `sync` recipe is and
dic_sync/fetchers/__init__.py for how a catalog shape is added.
"""
import argparse, os, sys

from dic_sync import catalog


def main(argv=None):
    """The CLI: parse flags, run the driver, name the file and its count."""
    parser = argparse.ArgumentParser(
        prog="dic-sync",
        description="fetch each provider's model list")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="print nothing to stderr but a failure")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="extra detail on stderr")
    parser.add_argument("-p", "--provider", action="append", metavar="NAME",
                        help="only fetch this provider's catalog;"
                             " repeatable")
    args = parser.parse_args(argv)

    def repaint(text):
        """Overwrite the live progress line in place; --quiet writes none."""
        if args.quiet:
            return
        sys.stderr.write(f"\r\033[Kdic-sync: {text}")
        sys.stderr.flush()

    def report(level, text):
        if args.quiet and level >= 0:
            return
        if level > 0 and not args.verbose:
            return
        # end whatever live line is under this one first, so the
        # message lands where the meter was and not after it
        if not args.quiet:
            sys.stderr.write("\r\033[K")
        print(f"dic-sync: {text}", file=sys.stderr)

    try:
        path, count = catalog.sync(os.environ, report, args.provider,
                                   repaint)
    except Exception as e:  # every failure, so the exit status is always one
        print(f"dic-sync: {e}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{path}: {count} model(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
