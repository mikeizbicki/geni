"""A published unit and rate as dic's own vocabulary.

The catalog says what a price is per -- a plural noun ("seconds",
"images"), a decimal string, a cent-string -- and dic's price table has
one shape: a rate per a unit, quoted per million for a token and per one
for everything else.  The translation lives here, so a fetcher asks for
the conversion it needs by name and no fetcher invents a rule.

The mirror of this module inside dic is price.py; a change here that
price.py no longer reads is a rule that costs nothing silently.

>>> plural("seconds"), plural("images")
('second', 'image')
>>> plural("second"), plural("image")
('second', 'image')
"""

PER_MILLION = 10 ** 6


def plural(unit):
    """A plural unit name as its singular, and a singular as itself.

    >>> plural("seconds"), plural("second"), plural("tokens")
    ('second', 'second', 'token')
    """
    return unit[:-1] if unit.endswith("s") else unit


def per_token(rate):
    """A per-token dollar rate as the per-million number dic stores.

    >>> per_token("0.000001"), per_token(0.000005)
    (1.0, 5.0)
    """
    return float(rate) * PER_MILLION


def cents(rate):
    """A cent-string as dollars.

    >>> cents("2"), cents("0.5")
    (0.02, 0.005)
    """
    return float(rate) / 100
