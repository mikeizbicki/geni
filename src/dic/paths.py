"""Where dic's own files are, in a checkout and in an installed package.

`dic.sh` and the scripts beside it are bash, and two callers need a path to
them: `--init`, which prints the `source` line a .bashrc evals, and
`mcp.command`, which puts the sandbox in front of every `--mcp` command it
runs.  Neither derives the path for itself, because the rule is one rule.

An installed dic carries the scripts as the package `dic.scripts`, which
setuptools makes by mapping the top-level `scripts/` directory onto it;
`importlib.resources` names it in every install layout.  A checkout has no
such package -- the mapping is what installing does -- so the fallback is
`scripts/` beside `src/`, written from `__file__` and never from the
working directory.
"""
import os


def scripts_dir():
    """The directory holding dic.sh, wherever dic was installed from.

    >>> os.path.basename(scripts_dir())
    'scripts'
    >>> os.path.isfile(os.path.join(scripts_dir(), "dic.sh"))
    True
    """
    try:
        from importlib.resources import files
        return str(files("dic.scripts"))
    except ModuleNotFoundError:
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.normpath(os.path.join(here, os.pardir, os.pardir,
                                             "scripts"))
