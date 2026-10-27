# netsandbox.sh -- a thin wrapper around sandbox(1) that adds
# fine-grained network controls.
#
# WHY
#
# sandbox.sh gives a payload a private network namespace with no route
# off the machine (--unshare-net).  That is the strongest default: a
# file that was never mounted cannot be read, and a network that does
# not exist cannot be reached.  But it is all-or-nothing.  A build
# that fetches a dependency, or a test that calls an API, has to be
# run with --share-net, which hands the payload the host's network
# stack and every credential and service reachable from it.
#
# netsandbox fills the gap.  The payload still runs in a network
# namespace of its own, so nothing on the host is reachable by
# default, but one proxy inside that namespace is the only thing with
# a route out.  The proxy decides what may leave: by hostname, by
# method, by protocol.
#
# THREAT MODEL
#
# The threat model is the one in sandbox.sh: the payload is controlled
# by a literal devil, so nothing it prints or sends may be trusted,
# and the network is the one channel a payload has to the outside
# world.  A model that has read a secret and can reach any host can
# exfiltrate it; a model that can only reach api.example.com over GET
# cannot.
#
# The design keeps the trust floor where sandbox.sh put it: a kernel
# boundary.  The payload runs in a network namespace that has no route
# to the host's network stack.  The only path out is the proxy, and
# the proxy is the policy.  Nothing the payload can do -- no syscall,
# no capability, no /proc entry -- reaches the host's network except
# through the proxy.
#
# DESIGN
#
# 1. The netns is the jail.  `unshare -rn` creates a network
#    namespace with only loopback.  The payload's own bwrap
#    --unshare-net is dropped (--share-net is passed instead), so the
#    payload shares the outer netns -- which has no route out.
#
# 2. nftables redirects every TCP connection to the proxy.  A rule in
#    the nat/output chain rewrites the destination to 127.0.0.1:3128,
#    so a program that calls connect(2) to any address lands on the
#    proxy without knowing it.  The proxy recovers the original
#    destination with SO_ORIGINAL_DST and dials it.  Programs are
#    unmodified.
#
# 3. The proxy is the policy.  It reads the --net-* flags and decides
#    what may leave.  Host patterns are globs on the SNI/Host header,
#    not IPs -- rebind-safe by construction.  Methods apply only under
#    MITM; without --net-mitm they are a startup error, not silently
#    ignored.
#
# 4. HTTPS is the wall.  Method and hostname live inside TLS;
#    transparent mode must MITM, which means a CA in the sandbox's
#    trust store (/etc/ssl/certs is already a ro-bind target in
#    sandbox.sh).  Without that you only get SNI, which is
#    per-connection and breaks h2.
#
# 5. Escape requires a kernel bug in the netns/userns boundary -- the
#    same trust floor as bwrap itself.
#
# EXAMPLES
#
#     netsandbox --net-allow api.github.com \
#                --net-method GET,HEAD \
#                -- cargo build
#
#     netsandbox --net-deny POST,PUT,DELETE \
#                --net-allow '*.python.org' \
#                -- pytest
#
#     netsandbox --net-policy policy.json -- curl https://x/
#     netsandbox --net-off -- make
#     netsandbox --net-raw --share-net -- ...
#
# CLI
#
# Same shape as sandbox: leading NAME=value / --envs, then bwrap args,
# -- ends options.  Our flags occupy --net-*, a namespace bwrap does
# not use.  Semantics:
#
#   --net-allow HOST       allow HOST (glob); first match wins
#   --net-deny HOST        deny HOST
#   --net-method M[,M...]  allow only these methods (needs --net-mitm)
#   --net-deny-method M    deny these methods
#   --net-default allow|deny  tail policy (default deny)
#   --net-policy FILE      read rules from FILE (JSON)
#   --net-mitm             generate a CA and MITM TLS
#   --net-log FILE         one JSONL line per request
#   --net-off              no network (default; same as sandbox)
#   --net-raw              --share-net, no proxy (escape hatch)
#
# --net-* present implies netns + nft redirect + proxy.  Absent
# implies the current sandbox behavior (--unshare-net), no proxy.
#
# The --bind=SRC shorthand (bwrap does not support it; sandbox.sh
# rewrites it to --bind SRC SRC) is inherited: netsandbox calls
# sandbox, which handles it.  A direct `bwrap --bind=src` still
# fails, as it should -- bwrap is not the wrapper.
#
# WHAT IS NOT SETTLED
#
# - The proxy itself.  A ~150-line Go program on
#   net/http/httputil.ReverseProxy is the lightest auditable option;
#   `mitmproxy --mode transparent` is heavier but MITMs TLS out of the
#   box; `tinyproxy` and `squid` are C and have hostname allowlists
#   but no method filtering without MITM.
#
# - The exemption mechanism.  The proxy must reach the internet, but
#   nftables redirects all TCP.  Options: a cgroup the proxy runs in,
#   a packet mark, a sibling netns.  The draft assumes the proxy is
#   exempt by some mechanism and leaves it as a TODO.
#
# - The policy file format.  The draft writes a simple line-based
#   format; a JSON schema sibling to plan.jsonschema would be the
#   next step.

[[ -n ${_DIC_LOADED:-} ]] || source "${BASH_SOURCE[0]%/*}/dic.sh"

# --- policy ---

# Write the policy the proxy reads, on stdout.  One rule per line, in
# the order the caller gave them, so first-match-wins is preserved.
# The proxy is the only reader.
netsandbox-policy() {
    printf 'default %s\n' "$net_default"
    local host m
    for host in "${net_allow[@]}"; do
        printf 'allow host %s\n' "$host"
    done
    for host in "${net_deny[@]}"; do
        printf 'deny host %s\n' "$host"
    done
    for m in "${net_method_allow[@]}"; do
        printf 'allow method %s\n' "$m"
    done
    for m in "${net_method_deny[@]}"; do
        printf 'deny method %s\n' "$m"
    done
    [[ -n $net_log ]] && printf 'log %s\n' "$net_log"
}

# --- proxy ---

# Start the proxy.  In a real implementation this would be a small Go
# program on net/http/httputil.ReverseProxy (auditable, no deps) or
# `mitmproxy --mode transparent` (heavier, MITMs TLS).  For the draft,
# we assume `netsandbox-proxy` is on PATH and reads its policy from
# the file named by $1.
netsandbox-proxy() {
    if ! command -v netsandbox-proxy >/dev/null 2>&1; then
        dic-error "netsandbox-error: netsandbox-proxy not found"
        dic-hint "netsandbox-hint: install a transparent proxy, or use --net-off"
        return 1
    fi
    netsandbox-proxy --policy "$1" --listen 127.0.0.1:3128
}

# --- validate ---

# Pure: no model call, no ref moved.  A run that cannot work is
# refused before a netns is created or a token is spent.
netsandbox-validate() {
    local -a other=("${net_allow[@]}" "${net_deny[@]}"
                    "${net_method_allow[@]}" "${net_method_deny[@]}")
    if (( net_off )) && [[ -n $net_policy || $net_mitm || -n $net_log || ${#other[@]} ]]; then
        dic-error "netsandbox-error: --net-off contradicts the other --net-* flags"
        return 1
    fi
    if (( net_raw )) && [[ -n $net_policy || $net_mitm || -n $net_log || ${#other[@]} ]]; then
        dic-error "netsandbox-error: --net-raw contradicts the other --net-* flags"
        return 1
    fi
    if (( ${#net_method_allow[@]} || ${#net_method_deny[@]} )) && (( ! net_mitm )); then
        dic-error "netsandbox-error: --net-method needs --net-mitm"
        dic-hint "netsandbox-hint: without MITM, only the hostname (SNI) is visible"
        return 1
    fi
    case $net_default in
        allow|deny) ;;
        *) dic-error "netsandbox-error: --net-default must be allow or deny"
           return 1 ;;
    esac
    if [[ -n $net_policy && ! -r $net_policy ]]; then
        dic-error "netsandbox-error: $net_policy: cannot read"
        return 1
    fi
    return 0
}

# --- run ---

# The whole run happens inside a fresh network namespace.  unshare
# creates it; -r maps us to root inside a new user namespace, which is
# what lets an unprivileged user create the netns and install the
# nftables rules.  The payload's own bwrap --unshare-net is dropped
# (--share-net is passed instead), so the payload shares the outer
# netns -- which has no route out.
#
# Inside the netns, nftables redirects every TCP connection to the
# proxy.  A program that calls connect(2) to any address lands on
# 127.0.0.1:3128 without knowing it; the proxy recovers the original
# destination with SO_ORIGINAL_DST and dials it.  The proxy is the
# only thing in the netns with a route out, and it is exempt from the
# redirect by a mechanism this draft leaves as a TODO (a cgroup the
# proxy runs in, a packet mark, or a sibling netns).
netsandbox-run() {
    local policy_file self
    policy_file=$(mktemp -t netsandbox-policy.XXXXXX) || return 1
    netsandbox-policy > "$policy_file" || {
        rm -f "$policy_file"
        return 1
    }
    self=$(realpath "${BASH_SOURCE[0]}") || {
        rm -f "$policy_file"
        return 1
    }

    # bash -c gets a fresh shell with no functions of its own, so the
    # script sources netsandbox.sh (which sources dic.sh in turn) to
    # reach sandbox and netsandbox-proxy.  The path is resolved before
    # the unshare, because the child's cwd may differ.
    unshare -rn bash -c '
        set -e
        source "$1"
        nft add table ip nat
        nft add chain ip nat out "{ type nat hook output priority 0; }"
        nft add rule ip nat out meta l4proto tcp redirect to :3128
        nft add rule ip nat out drop
        netsandbox-proxy "$2" &
        proxy_pid=$!
        trap "kill $proxy_pid 2>/dev/null" EXIT
        sandbox --share-net "${@:3}"
    ' -- "$self" "$policy_file" "${rest[@]}"
    local status=$?
    rm -f "$policy_file"
    return "$status"
}

# --- entry ---

netsandbox() {
    local -a net_allow=() net_deny=() net_method_allow=() net_method_deny=()
    local net_default=deny net_policy= net_mitm=0 net_log=
    local net_off=0 net_raw=0
    local -a rest=()
    local m
    while (( $# )); do
        case $1 in
            --net-allow)       net_allow+=("$2"); shift 2 ;;
            --net-allow=*)     net_allow+=("${1#*=}"); shift ;;
            --net-deny)        net_deny+=("$2"); shift 2 ;;
            --net-deny=*)      net_deny+=("${1#*=}"); shift ;;
            --net-method)      IFS=, read -ra m <<< "$2"; net_method_allow+=("${m[@]}"); shift 2 ;;
            --net-deny-method) IFS=, read -ra m <<< "$2"; net_method_deny+=("${m[@]}"); shift 2 ;;
            --net-default)     net_default=$2; shift 2 ;;
            --net-policy)      net_policy=$2; shift 2 ;;
            --net-mitm)        net_mitm=1; shift ;;
            --net-log)         net_log=$2; shift 2 ;;
            --net-off)         net_off=1; shift ;;
            --net-raw)         net_raw=1; shift ;;
            --)                rest+=("$@"); break ;;
            --bind=*|--ro-bind=*|--dev-bind=*)
                               rest+=("${1%%=*}" "${1#*=}" "${1#*=}"); shift ;;
            *)                 rest+=("$1"); shift ;;
        esac
    done

    netsandbox-validate || return $?

    if (( net_off )); then
        sandbox "${rest[@]}"
        return $?
    fi
    if (( net_raw )); then
        sandbox --share-net "${rest[@]}"
        return $?
    fi
    netsandbox-run
}
