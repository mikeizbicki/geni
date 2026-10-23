# netfilter.sh -- run a command in a network namespace whose only egress
# is to a set of allowed hostnames.
#
# WHY
#
# sandbox.sh gives a payload a private network namespace with no route
# off the machine (--unshare-net).  Strong, but all-or-nothing: a build
# that fetches a dependency, or a test that calls an API, needs
# --share-net, which hands the payload the host's whole network stack.
#
# netfilter fills the gap.  The payload still runs in a network namespace
# of its own, so nothing on the host is reachable by default, and exactly
# one thing -- a hostname from --net-allow-hosts -- is reachable by name.
#
# MECHANISM
#
# The unit of permission is an IP address, not an SNI string, so the
# filter is protocol-independent: once google.com has resolved, ping,
# ssh, http, and https to its address all pass, and the payload cannot
# distinguish an allowed host from any other.  This is deliberate.  An
# SNI filter cannot see ICMP or a non-TLS port, and an SNI that the
# payload chooses carries no security weight; an IP that dnsmasq put in
# the set does, because dnsmasq put it there.
#
#   1. unshare -rmn makes a network namespace with only loopback, and a
#      mount namespace so that a resolv.conf can be bound over the
#      host's.  -r maps the caller to root inside a new user namespace,
#      which is what lets an unprivileged user create the netns, install
#      nftables rules and mount.
#
#   2. dnsmasq listens on 127.0.0.1:53 in that netns, forwards to the
#      host's real resolvers, and on every answer names the IP in an
#      nftables set (--nftset).  The set entry carries a timeout, so a
#      rotated address expires.
#
#   3. nftables drops everything in the output hook but:
#        - traffic to loopback (so dnsmasq is reachable),
#        - queries to the host's upstream resolvers (so dnsmasq can work),
#        - traffic to an address dnsmasq has resolved.
#
#   4. /etc/resolv.conf inside the netns is replaced by a bind mount of
#      a file that names only 127.0.0.1, so the payload's resolver is
#      dnsmasq and not the host's stub.
#
# WHAT THIS DOES NOT STOP
#
# A payload that skips DNS and dials an IP directly, or that sends SNI
# evil.com to an allowed IP, still gets through -- but only if that IP
# is already in the set from an earlier resolution of an allowed name.
# Disallowing that requires an SNI filter, which cannot see ICMP or a
# non-TLS port, and this script chose protocol coverage over that.
#
# A payload that queries the host's upstream resolver directly gets
# answers for any name, but the answers arrive in the payload's own
# stack and never reach the set; its subsequent connect(2) is dropped
# like any other.  It can learn addresses, not use them.
#
# DNS exfiltration remains: dnsmasq forwards any query the payload
# makes, so a name like secret.example.com leaks its own label.  dnsmasq
# could be told to drop unknown names instead of forwarding, which
# changes the failure mode from "resolves then blocks" to "NXDOMAIN",
# and is a policy decision left for later.
#
# CLI
#
#     netfilter --net-allow-hosts h1,h2 -- cmd arg...
#
# The first `--` ends netfilter's own flags; everything after is the
# command, verbatim.  A flag netfilter does not know is passed through
# to the command, per scripts/AGENTS.md, so a new --net-* flag can be
# added without re-editing every caller.
#
# EXAMPLES
#
#     netfilter --net-allow-hosts google.com -- ping google.com
#     netfilter --net-allow-hosts google.com -- ping bing.com   # blocked
#     netfilter --net-allow-hosts '*.github.com' -- git clone ...
#     netfilter --net-allow-hosts github.com -- \
#         sandbox -- cargo build
#
# DEPENDENCIES
#
# nftables and dnsmasq, both in /sbin on Debian.  unprivileged user
# namespaces enabled (kernel.unprivileged_userns_clone, on by default).
# netfilter-validate checks all three before a netns is created.
#
# WHAT IS NOT SETTLED
#
# - Wildcards.  dnsmasq's nftset spec takes a domain, and dnsmasq
#   matches it as a suffix with a leading dot, so 'github.com' covers
#   'api.github.com' as well.  A literal '*.github.com' is not a
#   suffix dnsmasq recognizes and is passed through unchanged; a
#   rewrite belongs here or in dnsmasq, not in the set.
#
# - The resolver's own policy.  Right now dnsmasq forwards everything
#   and only the connect is filtered; whether it should NXDOMAIN names
#   that are not on the allowlist is the DNS-exfiltration decision
#   above.

[[ -n ${_DIC_LOADED:-} ]] || source "${BASH_SOURCE[0]%/*}/dic.sh"

# --- environment ---

# Print the resolv.conf dnsmasq should read for its upstreams, on stdout.
# The host's, except when it names systemd-resolved's loopback stub: that
# address is a listener in the host's netns and is not reachable from the
# netns netfilter makes, so a copied file would give dnsmasq an upstream
# it cannot dial.  /run/systemd/resolve/resolv.conf is the same list of
# servers without the stub in front of them.
netfilter-resolv-source() {
    local stub=/etc/resolv.conf
    if grep -qE '^[[:space:]]*nameserver[[:space:]]+127\.0\.0\.53([[:space:]]|$)' \
            "$stub" 2>/dev/null &&
       [[ -r /run/systemd/resolve/resolv.conf ]]; then
        printf '%s\n' /run/systemd/resolve/resolv.conf
        return 0
    fi
    printf '%s\n' "$stub"
}

# Print the upstream nameservers from a resolv.conf, one address per
# line, for both the dnsmasq server list and the nft allow set.
netfilter-resolv-servers() {
    awk '/^[[:space:]]*nameserver/ { print $2 }' "$1"
}

# Resolve a binary, preferring PATH but falling back to /sbin, which is
# where Debian puts nft and dnsmasq and is not always on a user's PATH.
netfilter-bin() {
    local name=$1 fallback
    if command -v "$name" >/dev/null 2>&1; then
        command -v "$name"
        return 0
    fi
    for fallback in /sbin/$name /usr/sbin/$name /usr/bin/$name; do
        if [[ -x $fallback ]]; then
            printf '%s\n' "$fallback"
            return 0
        fi
    done
    return 1
}

# --- validate ---

# Pure: no model call, no ref moved, no netns.  Everything a run needs
# that a run could be refused for is checked here.
netfilter-validate() {
    local missing=() need bin
    for need in unshare awk grep; do
        command -v "$need" >/dev/null 2>&1 || missing+=("$need")
    done
    for need in nft dnsmasq; do
        netfilter-bin "$need" >/dev/null || missing+=("$need")
    done
    if (( ${#missing[@]} )); then
        dic-error "netfilter-error: not installed: ${missing[*]}"
        dic-hint "netfilter-hint: on Debian: apt install nftables dnsmasq-base"
        return 1
    fi

    if ! unshare -rn true 2>/dev/null; then
        dic-error 'netfilter-error: unprivileged user namespaces are disabled'
        dic-hint 'netfilter-hint: sysctl -w kernel.unprivileged_userns_clone=1'
        return 1
    fi

    local src
    src=$(netfilter-resolv-source)
    if ! netfilter-resolv-servers "$src" | grep -q .; then
        dic-error "netfilter-error: no nameserver in $src"
        return 1
    fi
    return 0
}

# --- run ---

# The inner script, written to a temp file so that the quoting of a page
# of nft rules does not have to survive two shells.  Environment:
#
#   NETFILTER_NFT           the nft binary
#   NETFILTER_DNSMASQ       the dnsmasq binary
#   NETFILTER_NFTSET4/6     the dnsmasq --nftset specs
#   NETFILTER_UPSTREAM_V4   list of upstream v4 addresses, space-separated
#   NETFILTER_UPSTREAM_V6   list of upstream v6 addresses, space-separated
#   NETFILTER_RESOLV        the resolv.conf to bind over /etc/resolv.conf
#   NETFILTER_HOST_RESOLV   the resolv.conf dnsmasq reads for its servers

netfilter-inner() {
    cat <<'INNER'
set -e

ip link set lo up

# A resolv.conf that names only dnsmasq, bound over the host's.  The
# payload's resolver is then dnsmasq by construction, whether the
# payload is a libc that reads /etc/resolv.conf or a program that
# shells out to getent.  Mounted here and not prepared outside,
# because the file has to live in the netns's mount namespace.
mount --bind "$NETFILTER_RESOLV" /etc/resolv.conf

# ----- nft -----

"$NETFILTER_NFT" add table inet netfilter
"$NETFILTER_NFT" add set inet netfilter allowed \
    '{ type ipv4_addr; flags timeout; timeout 1h; }'
"$NETFILTER_NFT" add set inet netfilter allowed6 \
    '{ type ipv6_addr; flags timeout; timeout 1h; }'
"$NETFILTER_NFT" add set inet netfilter upstream \
    '{ type ipv4_addr; }'
"$NETFILTER_NFT" add set inet netfilter upstream6 \
    '{ type ipv6_addr; }'

# The host's resolvers are permitted by address and port 53 only, so
# dnsmasq can forward.  A payload that dials the same address on
# another port, or another address on port 53, falls through.
for ip in $NETFILTER_UPSTREAM_V4; do
    "$NETFILTER_NFT" add element inet netfilter upstream "{ $ip }"
done
for ip in $NETFILTER_UPSTREAM_V6; do
    "$NETFILTER_NFT" add element inet netfilter upstream6 "{ $ip }"
done

"$NETFILTER_NFT" add chain inet netfilter output \
    '{ type filter hook output priority 0; policy drop; }'
"$NETFILTER_NFT" add rule inet netfilter output oifname "lo" accept
"$NETFILTER_NFT" add rule inet netfilter output \
    ip daddr @upstream udp dport 53 accept
"$NETFILTER_NFT" add rule inet netfilter output \
    ip daddr @upstream tcp dport 53 accept
"$NETFILTER_NFT" add rule inet netfilter output \
    ip6 daddr @upstream6 udp dport 53 accept
"$NETFILTER_NFT" add rule inet netfilter output \
    ip6 daddr @upstream6 tcp dport 53 accept
"$NETFILTER_NFT" add rule inet netfilter output \
    ip daddr @allowed accept
"$NETFILTER_NFT" add rule inet netfilter output \
    ip6 daddr @allowed6 accept
# policy drop is the fallthrough: anything not named above is refused.

# ----- dnsmasq -----

# --no-resolv: the resolv.conf above names 127.0.0.1, and reading it
# would point dnsmasq at itself.  The host's file is passed by name.
# --user=root: dnsmasq asks to drop privileges by default, and inside
# a fresh user namespace the account it would drop to is not mapped.
# It is the trusted half of this setup; the payload is the half that
# must not have capabilities, and the nested namespace below is what
# takes them away.
#
# --nftset populates the set on a forwarded answer and on nothing else.
# A dnsmasq told to answer from --address= synthesizes locally and never
# reaches this code; that is why the upstream below is the host's own
# resolver and not a static table, and why a later "simplification" to
# --address= would silently empty the set.
#
# shellcheck disable=SC2086  # word-splitting of the --server list is intended
"$NETFILTER_DNSMASQ" --no-daemon --keep-in-foreground \
    --user=root \
    --port=53 --bind-interfaces --listen-address=127.0.0.1 \
    --no-resolv --resolv-file="$NETFILTER_HOST_RESOLV" \
    --nftset="$NETFILTER_NFTSET4" \
    --nftset="$NETFILTER_NFTSET6" &
DNSMASQ_PID=$!
trap 'kill "$DNSMASQ_PID" 2>/dev/null || true' EXIT

# dnsmasq needs a moment to bind; the payload's first getaddrinfo must
# not race it, or the run fails on a cold cache before anything has had
# a chance to be permitted.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if "$NETFILTER_NFT" list set inet netfilter allowed >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
sleep 0.2

# A nested user namespace is what takes the capabilities away.  The
# rules above live in the netns's userns; a process in a child userns
# has no capabilities there, so it can neither flush the rules nor
# install its own, and --pid below keeps it from seeing dnsmasq to
# signal it.  setpriv is not this: dropping from the bounding set only
# affects what a setuid exec could reacquire, and leaves the
# effective and permitted sets alone.
exec unshare --user --map-root-user --pid --fork -- "$@"
INNER
}

netfilter-run() {
    local allow=$1; shift
    local -a hosts=()
    local h joined=
    IFS=, read -ra hosts <<< "$allow"
    for h in "${hosts[@]}"; do
        [[ -n $h ]] && joined+="/$h"
    done
    if [[ -z $joined ]]; then
        dic-error 'netfilter-error: --net-allow-hosts is empty'
        return 1
    fi

    local host_resolv v4 v6 nft dnsmasq
    host_resolv=$(netfilter-resolv-source)
    v4=$(netfilter-resolv-servers "$host_resolv" | grep -v ':' | tr '\n' ' ')
    v6=$(netfilter-resolv-servers "$host_resolv" | grep ':' | tr '\n' ' ')

    nft=$(netfilter-bin nft) || return 1
    dnsmasq=$(netfilter-bin dnsmasq) || return 1

    # Scratch for this run: the payload's resolv.conf and the inner
    # script.  Removed on every return, so a failed run leaves nothing
    # behind for a later one to trip on.
    local tmpd
    tmpd=$(mktemp -d "${TMPDIR:-/tmp}/netfilter.XXXXXXXX") || return 1
    trap 'rm -rf "$tmpd"' RETURN

    local payload_resolv=$tmpd/resolv.conf
    printf 'nameserver 127.0.0.1\n' > "$payload_resolv" || return 1

    local inner=$tmpd/inner.sh
    netfilter-inner > "$inner" || return 1

    local status
    NETFILTER_NFT="$nft" \
    NETFILTER_DNSMASQ="$dnsmasq" \
    NETFILTER_NFTSET4="${joined}/4#inet#netfilter#allowed" \
    NETFILTER_NFTSET6="${joined}/6#inet#netfilter#allowed6" \
    NETFILTER_UPSTREAM_V4="$v4" \
    NETFILTER_UPSTREAM_V6="$v6" \
    NETFILTER_RESOLV="$payload_resolv" \
    NETFILTER_HOST_RESOLV="$host_resolv" \
    dic-run unshare -rmn bash "$inner" -- "$@"
    status=$?
    return "$status"
}

# --- entry ---

netfilter() {
    # exit codes:
    # 0   the command ran (whatever it returned)
    # 1   the jail could not be built
    # 64  usage error
    #
    # netfilter's own flags are parsed first; the first `--` ends them
    # and everything after is the command, verbatim.  An argument that
    # is not one of netfilter's flags is the command, so the `--` is
    # optional for a simple case and is required only when the command
    # itself begins with a flag netfilter also knows.
    local allow=
    local -a command=()
    while (( $# )); do
        case $1 in
            --net-allow-hosts)
                if (( $# < 2 )); then
                    dic-error 'netfilter-error: --net-allow-hosts needs a value'
                    return 64
                fi
                allow=$2; shift 2 ;;
            --net-allow-hosts=*) allow=${1#*=}; shift ;;
            --) shift; command+=("$@"); break ;;
            *) command+=("$@"); break ;;
        esac
    done

    if [[ -z $allow ]]; then
        dic-error 'netfilter-error: --net-allow-hosts is required'
        dic-hint "netfilter-hint: usage: netfilter --net-allow-hosts h1,h2 -- cmd"
        return 64
    fi
    if (( ${#command[@]} == 0 )); then
        dic-error 'netfilter-error: no command'
        return 64
    fi

    netfilter-validate || return $?
    netfilter-run "$allow" "${command[@]}"
}

# A test that sources this file gets the functions; a shell that runs it
# gets the command.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    netfilter "$@"
fi
