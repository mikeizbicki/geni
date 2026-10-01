"""Rating: what one call's usage costs under the model's price table.

    dic/price.py    the price rules, and the one sum over them

A price table is one JSON object of named rules.  A rule's name is the usage
name it rates, or a prefix of one, in which case it rates everything under that
prefix: 'in' prices every input token, and 'in.cache_read' prices only the ones
the provider served from its cache.  A rule that prices only *part* of a
request -- the second rate for a prompt past a long context threshold -- says
so with `key`, naming the same usage name as the rule it refines.

Batching, flex and priority tiers and that threshold are all properties of the
*request* and not of a token in it, so a rule matches them against the facts of
the call and not against the name.

The most specific matching rule wins.  Two rules that match equally well at two
different rates are an error, because a silently picked price is a wrong
invoice.

`rate` is the only sum in dic.  It runs once, when the call ends, and its
result is stored: usage is a fact about a call that can be read again forever,
and the table that rated it is a config file that the next invocation may have
edited, so the dollars it produced are recorded rather than recomputed.
"""
import hashlib, json
import ast, operator
from dic.tty import DicError

# What a rate is quoted in.  A token is sold by the million and everything else
# by the one, so a fal image at $0.04 and an OpenAI token at $3.00 can live in
# one table and one shape of rule.
PER = {"token": 10 ** 6}


def facts(usage, tier="default"):
    """What a rule's `when` and `tier` are matched against: the call's own.

    A rule says what the whole call costs -- a prompt over 200k tokens is
    priced differently because the *request* was long, not because one token in
    it was -- so the facts carry the totals as well as every usage name.  A
    `when` is a python expression over these names, evaluated with no builtins:
    `in_total > 200000` is a rule and `import os` is an error.

    >>> facts({"in": 100, "in.cache_read": 900, "out": 5})["in_total"]
    1000
    >>> facts({}, tier="batch")["tier"]
    'batch'
    """
    out = dict(usage, tier=tier)
    for prefix in ("in", "out"):
        out[f"{prefix}_total"] = sum(
            qty for key, qty in usage.items()
            if key == prefix or key.startswith(prefix + "."))
    return out


def segments(name):
    """A rule name or a usage name as its parts.

    >>> segments("in.cache_read")
    ['in', 'cache_read']
    """
    return name.split(".")


# The operations a `when` may use, so that a price rule is data and never
# code: eval would let a shared models.json reach every class the
# interpreter has loaded, and that is not a price table's business.
_OPS = {ast.Gt: operator.gt, ast.Lt: operator.lt,
        ast.GtE: operator.ge, ast.LtE: operator.le,
        ast.Eq: operator.eq, ast.NotEq: operator.ne,
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv}


def when(text, facts):
    """Evaluate one price rule's `when` against the facts of the call.

    The expression is a comparison, or a boolean combination of them, over
    the names in facts: no call, no attribute, no subscript, no name that
    is not a fact.  That is the whole language a price rule gets, because
    a config file is data and not code.

    >>> when("in_total > 200", {"in_total": 300})
    True
    >>> when("in_total > 200 and out_total < 10",
    ...      {"in_total": 300, "out_total": 5})
    True
    >>> when("1 + 1 == 2", {})
    True
    >>> when("__import__('os')", {})
    Traceback (most recent call last):
    ...
    dic.tty.DicError: price rule: Call not allowed in `when`
    >>> when("x.__class__", {"x": 1})
    Traceback (most recent call last):
    ...
    dic.tty.DicError: price rule: Attribute not allowed in `when`
    """
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in facts:
                raise DicError(
                    f"price rule: {node.id} is not a fact of the call")
            return facts[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.BoolOp):
            values = [walk(value) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = walk(node.left)
            for operation, comparator in zip(node.ops, node.comparators):
                if type(operation) not in _OPS:
                    raise DicError(f"price rule:"
                                   f" {type(operation).__name__} not"
                                   " allowed in `when`")
                right = walk(comparator)
                if not _OPS[type(operation)](left, right):
                    return False
                left = right
            return True
        raise DicError(f"price rule: {type(node).__name__}"
                       " not allowed in `when`")

    return bool(walk(ast.parse(text, mode="eval")))


def matches(key, name, rule, facts):
    """Whether the rule named `name` rates the usage name `key` in this call.

    The rule's pattern -- its `key`, or its own name -- must be a prefix of the
    usage name, so 'in' rates 'in.cache_read' and 'in.cache_read' does not rate
    'in': a refinement is more specific than what it refines, never the other
    way round.

    >>> matches("in.cache_read", "in", {}, {})
    True
    >>> matches("in.cache_read", "in.cache_write", {}, {})
    False
    >>> matches("in", "in.long", {"key": "in", "when": "in_total > 200"},
    ...         {"in_total": 300})
    True
    >>> matches("in", "in.long", {"key": "in", "when": "in_total > 200"},
    ...         {"in_total": 100})
    False
    """
    parts = segments(rule.get("key", name))
    if segments(key)[:len(parts)] != parts:
        return False
    if rule.get("tier") and rule["tier"] != facts.get("tier"):
        return False
    condition = rule.get("when")
    return not condition or when(condition, facts)


def specificity(name, rule):
    """How much a rule constrains, so that the most constrained one can win.

    More of the usage name, then a tier, then a condition.  A rule that says
    more about the call is the rule that prices it.

    >>> specificity("in", {}), specificity("in.cache_read", {"when": "1"})
    ((1, False, False), (2, False, True))
    """
    return (len(segments(rule.get("key", name))), bool(rule.get("tier")),
            bool(rule.get("when")))


def rated(key, rules, facts):
    """The rule that prices one usage name, or (None, None) if none does.

    >>> rules = {"in": {"rate": 3.0}, "in.cache_read": {"rate": 0.3}}
    >>> rated("in.cache_read", rules, {})
    ('in.cache_read', {'rate': 0.3})
    >>> rated("in", rules, {})
    ('in', {'rate': 3.0})
    >>> rated("out", rules, {})
    (None, None)
    """
    found = sorted(((specificity(name, rule), name, rule)
                    for name, rule in rules.items()
                    if matches(key, name, rule, facts)), reverse=True)
    if not found:
        return None, None
    best, name, rule = found[0]
    for other, other_name, other_rule in found[1:]:
        if other == best and other_rule.get("rate") != rule.get("rate"):
            raise DicError(
                f"{key}: {name} prices it at {rule.get('rate')} and"
                f" {other_name} at {other_rule.get('rate')};"
                " a rule that cannot be told from another cannot be priced")
    return name, rule


def rate(model, usage, facts):
    """(total, items): what usage costs under model's rules, item by item.

    One item per priced usage name: what it was, how many of it, at what rate,
    and what that came to.  A name no rule prices costs nothing and is left
    out, so a model with no price table costs nothing at all -- which is what
    dic has always said about one.

    >>> model = {"price": {"in": {"rate": 3.0}, "out": {"rate": 15.0}}}
    >>> usage = {"in": 1000, "out": 500}
    >>> total, items = rate(model, usage, facts(usage))
    >>> round(total, 6), items[0]
    (0.0105, {'rule': 'in', 'key': 'in', 'qty': 1000, 'rate': 3.0, 'cost': 0.003})
    >>> rate({}, usage, facts(usage))
    (0.0, [])
    """
    items, total = [], 0.0
    for key, qty in sorted(usage.items()):
        name, rule = rated(key, model.get("price") or {}, facts)
        if name is None:
            continue
        cost = rule["rate"] * qty / PER.get(rule.get("unit", "token"), 1)
        items.append({"rule": name, "key": key, "qty": qty,
                      "rate": rule["rate"], "cost": cost})
        total += cost
    return total, items


# The rule name a provider-reported charge is stored under, and the value
# `price_hash` takes for a row that was invoiced rather than estimated: one
# name, so --stats counts the two separately without a second column.
REPORTED = "provider"


def reported(total, estimate=None):
    """(total, items) for a call the provider itself priced.

    A provider that says what it charged is the ground truth, so the price
    table is not consulted and the dollars are taken as given.  The estimate
    the table would have produced is kept beside them, so a table that has
    drifted from the invoice is visible rather than assumed; nothing reads it
    back, because a bill is stored and never recomputed.

    >>> total, items = reported(0.0245)
    >>> total, items[0]["cost"], items[0]["rule"]
    (0.0245, 0.0245, 'provider')
    >>> reported(0.0245, 0.02)[1][0]["estimate"]
    0.02
    >>> reported(0.0)[1][0]["cost"]        # a call the provider made free
    0.0
    """
    item = {"rule": REPORTED, "key": "cost", "qty": total, "rate": 1.0,
            "cost": total}
    if estimate is not None:
        item["estimate"] = estimate
    return total, [item]


def merged(rounds):
    """(usage, items): several rounds' billing as though it were one call.

    A tool loop is several paid requests for one answer, so the line a user
    reads is the sum of them: the quantities add up by name, and the
    itemizations merge rule by rule, so a call of one round and a call of five
    read the same.

    >>> model = {"price": {"in": {"rate": 3.0}}}
    >>> a = ({"in": 1000}, *rate(model, {"in": 1000}, facts({"in": 1000})))
    >>> b = ({"in": 500}, *rate(model, {"in": 500}, facts({"in": 500})))
    >>> usage, items = merged([a, b])
    >>> usage["in"], items[0]["qty"], round(items[0]["cost"], 6)
    (1500, 1500, 0.0045)
    """
    usage, items = {}, {}
    for round_usage, _, round_items in rounds:
        for name, qty in round_usage.items():
            usage[name] = usage.get(name, 0) + qty
        for item in round_items:
            key = (item["rule"], item["key"], item["rate"])
            if key in items:
                items[key]["qty"] += item["qty"]
                items[key]["cost"] += item["cost"]
            else:
                items[key] = dict(item)
    return usage, list(items.values())


def price_hash(model):
    """A short name for the table a row was rated under, stored with the row.

    Nothing is ever repriced, so this is the only way a cost can say which
    table produced it after the config file has moved on; --stats counts the
    distinct ones, so an average that mixes two tables is visible.

    >>> a = price_hash({"price": {"in": {"rate": 1}}})
    >>> a == price_hash({"price": {"in": {"rate": 1}}}), a == price_hash({})
    (True, False)
    """
    return hashlib.sha256(
        json.dumps(model.get("price") or {},
                   sort_keys=True).encode()).hexdigest()[:12]
