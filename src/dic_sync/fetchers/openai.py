"""The OpenAI-shaped listing: GET {base}/models, {"data": [{"id": ...}]}.

Groq, Cerebras, Together, vLLM, llama.cpp, ollama, OpenAI itself, and
Anthropic's own /v1/models all answer this shape.  A provider whose
catalog is one GET under one path is this module's fetcher and no new
code; a provider whose catalog is a different shape is a new module.

A catalog entry in the recipe names a URL and, optionally, the models
at that URL whose protocol is not the provider's own: OpenAI serves
its chat, image and video models from one /models, and the recipe is
where that one GET is told apart into three.  Two rules keep this
correct -- the recipe is the only place a pattern lives, and one URL
is fetched once however many entries name it -- so a new image model
OpenAI ships tomorrow is called correctly the first time a sync sees
it.

No price is fetched: an OpenAI-shaped listing does not report one, and
a table written from a guess is a bill dic never paid.  The published
price pages are scripts/prices.sh's job, and its output is a file the
user edits and dic reads.
"""
import fnmatch

from dic_sync import http
from dic_sync.ids import chain

# The keys a recipe catalog uses to route a model, and not to describe
# it: `path` names the URL to fetch, `match` a glob the model's own id
# must satisfy, `prefix` the chain the id lands under.  Everything
# else an entry carries -- api_type, output, and any future config key
# dic learns -- is a fact about the models it claimed and is merged
# into each of them verbatim.
ROUTING = ("path", "match", "prefix")


def claim(slug, rules):
    """The rule that claims one model id: the first whose match accepts it.

    A rule without a match is a catch-all, so a recipe lists its
    name-pattern rules first and leaves the blanket one last; a model
    no rule names lands under the catch-all and inherits the provider's
    own api_type, which is what a chat model wants.

    >>> rules = [{"match": "gpt-image-*", "api_type": "openai-images"},
    ...          {"match": "sora-*", "api_type": "openai-videos"},
    ...          {"api_type": "openai-responses"}]
    >>> claim("gpt-image-2", rules)["api_type"]
    'openai-images'
    >>> claim("sora-3", rules)["api_type"]
    'openai-videos'
    >>> claim("gpt-7", rules)["api_type"]
    'openai-responses'
    """
    for rule in rules:
        pattern = rule.get("match")
        if pattern is None or fnmatch.fnmatchcase(slug, pattern):
            return rule
    return {}


def config_for(slug, rule):
    """The model's config entry: its name, plus the rule's routing-exempt keys.

    Everything the catalog entry says besides the routing keys is a
    fact about the models it claimed, so it lands in each of them: a
    match on gpt-image-* with an api_type of openai-images is how the
    one /models listing says that those are images and not chat.

    >>> config_for("gpt-image-2", {"path": "/models",
    ...                            "match": "gpt-image-*",
    ...                            "api_type": "openai-images",
    ...                            "output": "image/png"})
    {'model_name': 'gpt-image-2', 'api_type': 'openai-images', 'output': 'image/png'}
    >>> config_for("gpt-7", {"path": "/models"})
    {'model_name': 'gpt-7'}
    """
    entry = {"model_name": slug}
    for key, value in rule.items():
        if key not in ROUTING:
            entry[key] = value
    return entry


def by_path(catalogs):
    """The recipe's catalogs grouped by the URL they fetch, in recipe order.

    Three rules over one /models is one GET read three ways, so the
    group is what makes a chat model, an image model and a video model
    arrive from a request the provider answers once.  Order within a
    group is the recipe's, which is what claim() reads.

    >>> by_path([{"path": "/models", "match": "a"}, {"path": "/models"}])
    [('/models', [{'path': '/models', 'match': 'a'}, {'path': '/models'}])]
    """
    order, groups = [], {}
    for catalog in catalogs:
        path = catalog["path"]
        if path not in groups:
            order.append(path)
            groups[path] = []
        groups[path].append(catalog)
    return [(path, groups[path]) for path in order]



def fetch(provider, entry, key, env, progress=None):  # noqa: ARG001
    """Every model one OpenAI-shaped catalog lists, as config entries.

    The path defaults to /models, which every provider of this shape
    serves; a catalog that lives elsewhere -- a provider whose own API
    is under a longer prefix -- names its path in the recipe, and a
    catalog that shares a host with another gets a prefix so the two
    do not collide in the config namespace.

    The api_base is the base of the call and not always the base of the
    catalog, so a recipe may override it with `catalog_base`; fal is the
    one that does.
    """
    recipe = entry["sync"]
    base = (recipe.get("catalog_base") or entry["api_base"]).rstrip("/")
    catalogs = recipe.get("catalogs") or [{"path": "/models"}]
    headers = http.auth(entry, key)
    out = {}
    for path, rules in by_path(catalogs):
        body = http.get(base + path, headers)
        for model in body.get("data") or []:
            slug = model["id"]
            rule = claim(slug, rules)
            prefix = rule.get("prefix") or provider
            out[chain(prefix, slug)] = config_for(slug, rule)
        if progress is not None:
            progress("listing", len(out))
    return out
