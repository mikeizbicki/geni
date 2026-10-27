"""Three catalogs, three pricing shapes, one provider.

OpenRouter publishes its chat, image and video models at three different
paths and prices them three different ways, so one fetcher covers the
provider:

  /models           pricing inline on the row, quoted per TOKEN as a
                    decimal string, with `overrides` for a long-context
                    threshold
  /images/models    pricing only at the per-model /endpoints
                    sub-resource, keyed by `billable`
                    (`output_image`, `input_image`, ...)
  /videos/models    pricing inline as `pricing_skus`, whose keys carry
                    the resolution tier and whose values are
                    cent-strings

Only the chat catalog's pricing is translated here.  Image pricing
costs one request per model and a sync would then be O(models) for one
provider; video pricing names a resolution the video adaptor does not
yet report in usage, so a tiered rule would match nothing and a flat
rule would be wrong for the tiers.  Both are named in a comment and
left out, which is a smaller lie than a price that does not match what
was bought.

The image and video catalogs are listed under `openrouter-image` and
`openrouter-video`, which are abstract entries that chain to an
api_type dic does not yet have for OpenRouter's shape.  The entries
therefore name a model a future adaptor can call and not one dic can
call today.
"""
from dic_sync import http, units
from dic_sync.ids import chain

# The TTL a wire `input_cache_write` is billed at.  OpenRouter's own
# caching docs do not publish separate rates for a five-minute write and
# an hour, so a row that names one write price names one TTL and the
# constant here is the one.  A models.json that changes it changes it
# for every entry this fetcher writes, and dic reads whatever is there.
CACHE_WRITE_TTL = "5m"


def chat_pricing(model):
    """One chat model's pricing as a dic rule table, or {} for none.

    A rate the wire does not publish is not written, and a rate the wire
    publishes as 0.0 is: a free tier is a price like any other, and
    dropping it would rewrite the catalog.

    >>> chat_pricing({"pricing": {
    ...     "prompt": "0.000001", "completion": "0.000005"}})
    {'in': {'rate': 1.0}, 'out': {'rate': 5.0}}
    >>> chat_pricing({"pricing": {
    ...     "prompt": "0.000001", "completion": "0.000005",
    ...     "overrides": [{"min_prompt_tokens": 272000,
    ...                    "prompt": "0.000002",
    ...                    "completion": "0.0000075"}]}})
    {'in': {'rate': 1.0}, 'out': {'rate': 5.0}, 'in.long': {'key': 'in', 'rate': 2.0, 'when': 'in_total > 272000'}, 'out.long': {'key': 'out', 'rate': 7.5, 'when': 'in_total > 272000'}}
    >>> chat_pricing({"pricing": {}})
    {}
    """
    pricing = model.get("pricing") or {}
    out = {}
    if pricing.get("prompt") is not None:
        out["in"] = {"rate": units.per_token(pricing["prompt"])}
    if pricing.get("completion") is not None:
        out["out"] = {"rate": units.per_token(pricing["completion"])}
    if pricing.get("input_cache_read") is not None:
        out["in.cache_read"] = {
            "rate": units.per_token(pricing["input_cache_read"])}
    if pricing.get("input_cache_write") is not None:
        out[f"in.cache_write.{CACHE_WRITE_TTL}"] = {
            "rate": units.per_token(pricing["input_cache_write"])}
    for override in pricing.get("overrides") or []:
        boundary = override.get("min_prompt_tokens")
        if boundary is None:
            continue
        condition = f"in_total > {boundary}"
        if override.get("prompt") is not None:
            out["in.long"] = {"key": "in",
                              "rate": units.per_token(override["prompt"]),
                              "when": condition}
        if override.get("completion") is not None:
            out["out.long"] = {"key": "out",
                               "rate": units.per_token(override["completion"]),
                               "when": condition}
    return out


def _catalog(base, path, headers):
    """Every model dict one OpenRouter listing returns, from its data array."""
    return http.get(base + path, headers).get("data") or []


def chat(base, headers, provider):
    """The chat catalog, its pricing translated into dic's own rules."""
    out = {}
    for model in _catalog(base, "/models", headers):
        slug = model["id"]
        config = {"model_name": slug}
        price = chat_pricing(model)
        if price:
            config["price"] = price
        out[chain(provider, slug)] = config
    return out


def images(base, headers, provider):
    """The image catalog, listed but not priced (see the module docstring).

    The prefix is the provider's own with `-image` after it, which is
    the name the abstract entry `openrouter-image` lives under; a model
    that chains to it inherits the image api_type and the catalog's
    own base without a second piece of config saying so.
    """
    out = {}
    for model in _catalog(base, "/images/models", headers):
        slug = model["id"]
        out[chain(provider + "-image", slug)] = {"model_name": slug}
    return out


def videos(base, headers, provider):
    """The video catalog, listed but not priced (see the module docstring)."""
    out = {}
    for model in _catalog(base, "/videos/models", headers):
        slug = model["id"]
        out[chain(provider + "-video", slug)] = {"model_name": slug}
    return out


def fetch(provider, entry, key, env, progress=None):  # noqa: ARG001
    """The three catalogs of one OpenRouter account, as config entries."""
    base = entry["api_base"].rstrip("/")
    headers = http.auth(entry, key)
    out = {}
    for name, fn in (("chat", chat), ("images", images), ("videos", videos)):
        before = len(out)
        out.update(fn(base, headers, provider))
        if progress is not None:
            progress(name, len(out) - before)
    return out
