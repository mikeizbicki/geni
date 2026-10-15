"""Selection: which of the traversed chunks survive.

Today it is the identity -- every chunk the walk yields is a chunk that
is rendered, which is why `accio src/` feels like no flag at all.  The
shapes this module will hold are the lenses the SPEC names, and the seam
is here so that each one is additive rather than a second pipeline.
"""


def select(nodes):
    """The identity lens: everything traversed is selected.

    >>> list(select([1, 2, 3]))
    [1, 2, 3]
    """
    return nodes
