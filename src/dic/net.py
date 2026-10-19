"""One network request whose waits are visible: dns, then the caller's own.

`dns` is separated from the wait that follows it because getaddrinfo never
returns to the interpreter while it runs, so a signal that arrives during it
is not handled until it is over: a resolver that has stopped answering is
ten seconds in which ^C does nothing and no clock can tick.  The resolver
therefore runs on a daemon thread, and the label returns to the caller's own
-- `ttft` for a stream of tokens, `mcp-handshake` for a JSON-RPC exchange --
once the addresses are in hand.  The clock is not restarted between them,
because a dns wait and the request that follows it are one wait from the
user's side, and the addresses are handed to the socket so the lookup is
never done twice.

Every network call dic makes goes through here, because a stock urlopen
resolves inside that same uninterruptible C call and so can paint no `dns:`
line at all.
"""
import http.client, socket, threading, urllib.parse

from dic.tty import DicError


def resolve(host, port, line=None, label="dns"):
    """Resolve host:port on a daemon thread, painted as `label: seconds  host`.

    A wait that ends in an OSError is raised here as a DicError rather than
    as the socket's own, so a caller sees why its call never reached the
    network and not the address family it never asked about.
    """
    if line is not None:
        line.waiting_for(label, host)
    box = {}

    def lookup():
        try:
            box["addresses"] = socket.getaddrinfo(
                host, port, proto=socket.IPPROTO_TCP)
        except OSError as e:
            box["error"] = e

    thread = threading.Thread(target=lookup, daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(0.1)
    if "error" in box:
        raise DicError(f"cannot resolve {host}: {box['error']}")
    return box["addresses"]


def dial(addresses):
    """An http.client _create_connection that connects to addresses in hand.

    The resolver already ran, so a second lookup here would be one more wait
    nobody can interrupt; the tuples are tried in order and the first that
    answers wins, which is what socket.create_connection would have done.
    """
    def connect(address, timeout=None, source_address=None):  # noqa: ARG001
        error = None
        for _, _, _, _, sockaddr in addresses:
            try:
                return socket.create_connection(sockaddr[:2], timeout,
                                                source_address)
            except OSError as e:
                error = e
        raise error
    return connect


def connection(url, line=None, label="ttft", timeout=None):
    """An http.client connection to url whose dns wait is `line`.

    `label` names the wait the caller is about to make -- ttft for a stream
    of tokens, mcp-handshake for a JSON-RPC exchange -- so the one line tells
    the two apart.  The scheme decides the connection class and the host and
    port are resolved here rather than by the socket, so the dns clock has
    somewhere to tick.
    """
    parts = urllib.parse.urlparse(url)
    secure = parts.scheme != "http"
    addresses = resolve(parts.hostname, parts.port or (443 if secure else 80),
                        line)
    if line is not None:
        line.waiting_for(label, parts.hostname)
    cls = (http.client.HTTPConnection if parts.scheme == "http"
           else http.client.HTTPSConnection)
    conn = cls(parts.netloc, timeout=timeout)
    conn._create_connection = dial(addresses)
    return conn
