"""The shell scripts dic ships, so `dic --init` can name one.

This file exists only so that setuptools treats scripts/ as a package: it
is installed as dic/scripts/, so `importlib.resources.files("dic.scripts")`
finds dic.sh for a pip-installed dic.  bash reads the .sh files here as
text and never imports this module.
"""
