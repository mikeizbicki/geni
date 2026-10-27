"""One GET or POST, JSON back: every failure a RuntimeError with a message.

dic-sync is not on a latency path and pays no price for urllib, so it
does not borrow dic.net's own connection and its dns progress row.  It
does borrow dic.store.http_url's refusal of a non-http scheme, because
a URL that arrived from a provider's own response is still a URL some
response chose, and file: is a URL urllib would honor.

The one thing urllib gives back that dic's own transport does not is an
error body, so it is read and folded into the message: a 401 that names
the missing variable is the difference between a five-minute fix and an
hour of guessing.
"""
import json, time, urllib.error, urllib.request

from dic.store import http_url

TIMEOUT_S = 30

# A 429 or a 5xx says come back later: a sync that gave up on the first
# one would drop a whole provider's catalog over a rate limit the
# provider expects to be waited out.  Five tries with exponential
# backoff is about 30s of waiting, which is what a per-minute limit
# needs; a Retry-After the server sent wins over the schedule, because
# it knows how long it means.
RETRIES = 5
BACKOFF_S = 2


def auth(entry, key):
    """The headers this provider's own catalog endpoint wants.

    The wire protocol decides: Anthropic's catalog wants a key header
    and a version, fal wants `Key` in front of its token, and every
    OpenAI-shaped listing wants `Bearer`.  The protocol is the entry's
    api_type and not the vendor's name, which is what makes a provider
    dic has never heard of work without a line of code.

    >>> auth({"api_type": "anthropic-messages"}, "k")["x-api-key"]
    'k'
    >>> auth({"api_type": "fal"}, "k")["authorization"]
    'Key k'
    >>> auth({}, "k")["authorization"]
    'Bearer k'
    """
    api_type = entry.get("api_type") or "openai-chat"
    if api_type == "anthropic-messages":
        return {"x-api-key": key, "anthropic-version": "2023-06-01",
                "accept": "application/json"}
    if api_type == "fal":
        return {"authorization": "Key " + key, "accept": "application/json"}
    return {"authorization": "Bearer " + key, "accept": "application/json"}


def get(url, headers, timeout=TIMEOUT_S):
    """GET url with headers and decode the body as JSON."""
    return _open(urllib.request.Request(http_url(url), headers=headers),  # noqa: S310
                 timeout)

def post(url, body, headers, timeout=TIMEOUT_S):
    """POST a JSON body and decode the reply as JSON."""
    data = json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310
        http_url(url), data=data,
        headers={**headers, "content-type": "application/json"})
    return _open(request, timeout)


def _open(request, timeout):
    """One request, one decoded body, one message on any failure.

    A 429 or a 5xx is retried up to RETRIES times, honouring a
    Retry-After the server sent; the last failure is what the caller
    sees, so a rate limit that outlives the retry is still reported.
    """
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(  # noqa: S310
                    request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            attempt += 1
            if attempt < RETRIES and _retryable(e.code):
                after = e.headers.get("Retry-After") if e.headers else None
                time.sleep(_delay(attempt, after))
                continue
            raise RuntimeError(f"{e.code} {request.full_url}:"
                               f" {_short(body)}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"{request.full_url}: {e}") from None


def _retryable(code):
    """Whether a status code is the server asking the caller to come back.

    >>> _retryable(429), _retryable(503), _retryable(404)
    (True, True, False)
    """
    return code == 429 or 500 <= code < 600


def _delay(attempt, after=None):
    """Seconds to wait before retry attempt n, or the server's Retry-After.

    >>> _delay(1), _delay(2), _delay(4)
    (2, 4, 16)
    >>> _delay(1, "5")
    5.0
    >>> _delay(1, "10")
    10.0
    """
    if after:
        try:
            return min(float(after), 60.0)
        except ValueError:
            pass
    return min(BACKOFF_S * 2 ** (attempt - 1), 30)


def _short(body):
    """A provider's error body as one short message.

    A Cloudflare 403 says what it means inside a JSON document and a
    bare truncation cuts it mid-word: the message field is what a
    reader needs, and a body that is not JSON keeps its head.

    >>> _short('{"message": "Rate limited"}')
    'Rate limited'
    >>> _short('{"detail": "blocked by the site owner"}')
    'blocked by the site owner'
    >>> _short('{"error": {"message": "nope"}}')
    'nope'
    >>> _short("plain text")[:5]
    'plain'
    """
    body = (body or "").strip()
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:800]
    if isinstance(parsed, dict):
        for key in ("message", "detail", "title", "error"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value[:800]
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"][:800]
    return body[:800]
