"""A provider's model id as a dic config id: one '+' chain, one safe segment.

A '/' in a model's own name is a namespace and not a chain delimiter, so
it becomes a '-'; a '+' would be read as another link in the chain and
would resolve to the wrong entry.  The mangling is injective over the
alphabet a provider uses in practice, so two distinct model ids mangle
to two distinct dic ids, which is what lets a rewrite of the synced
file mean one thing per source model.

>>> chain("groq", "qwen/qwen3.8-27b")
'groq+qwen-qwen3.8-27b'
>>> chain("openai", "gpt-6")
'openai+gpt-6'
>>> chain("openrouter-image", "openai/gpt-image-2")
'openrouter-image+openai-gpt-image-2'
"""


def safe(segment):
    """A model name's own character set, minus the two dic reads.

    >>> safe("qwen/qwen3.8-27b"), safe("a+b"), safe("plain")
    ('qwen-qwen3.8-27b', 'a-b', 'plain')
    """
    return segment.replace("/", "-").replace("+", "-")


def chain(prefix, slug):
    """One dic id from a provider prefix and a model's own name.

    >>> chain("fal", "fal-ai/nano-banana-pro")
    'fal+fal-ai-nano-banana-pro'
    """
    return f"{prefix}+{safe(slug)}"
