#!/bin/bash
#
# netsandbox.sh -- sandbox.sh with fine-grained network control.
#
# WHY
#
# sandbox.sh gives a payload a private network namespace with no
# route off the machine (--unshare-net).  That is the strongest
# default: a route that does not exist cannot be reached.  But it is
# all-or-nothing.  A build that fetches a dependency, or a test that
# calls an API, has to be run with --share-net, which hands the
# payload the host's whole network stack and every credential
# reachable from it.
#
# netsandbox fills the gap.  The payload still runs in a network
# namespace of its own, so nothing on the host is reachable by
# default, but a controlled egress is provided for named hosts.
#
# MODES
#
# With no --net-* flag, netsandbox is sandbox: --unshare-net and no
# route off the machine.  The absence of an option is what says "no
# network", so there is deliberately no --net-off.
#
#   --net-allow-hosts H,H
#
#       A dnsmasq in the jail is the only resolver the payload can
#       reach.  One --server rule per allowlisted name and a
#       --local=/#/ catch-all, so a query for a name not on the list
#       gets NXDOMAIN instead of a forwarded query, and the label of
#       an exfiltration attempt never leaves the machine.  Nothing is
#       pre-resolved: a listed name resolves on first lookup, and the
#       first lookup costs no more than any other.
#
#   --net-mitm [--net-method GET,HEAD | --net-deny-method POST]
#
#       HTTP(S)-only.  nftables dnat's the payload's egress on :80
#       and :443 to a per-run mitmproxy, which enforces the host list
#       from the Host header and TLS SNI, and --net-method filters by
#       HTTP method.  A method is inside the TLS record, so method
#       filtering requires interception and --net-mitm implies TLS
#       MITM, not SNI-only.
#
#   --net-raw
#
#       --share-net and no jail.  The escape hatch.  Refused
#       alongside any other --net-* flag.
#
# STRUCTURE (nested bwrap)
#
#     host -- pasta         (creates the netns and userns, and the
#           |                only route off them)
#           +-- bash inner.sh   (nft rules in the netns)
#                 |
#                 +-- bwrap #1  (filesystem sandbox for the daemons)
#                       |
#                       +-- sh -c 'dnsmasq & mitm & bwrap #2 ...'
#
# bwrap #1 renders a sandbox.sh-style map -- read-only /usr and /etc,
# a writable /run tmpfs -- and nothing else, so the CA private key,
# the host's $HOME, and /etc/ssh are not in the daemons' view.  The
# payload's bwrap #2 shares the netns (to reach dnsmasq on :53 and
# mitm on :8080) but runs as a different uid.
#
# THE UID EXEMPTION
#
# nft runs in the netns created by unshare -rmn, and meta skuid
# matches the uid as seen in that namespace.  unshare -r maps the
# caller to uid 0 there, and bwrap's --unshare-user defaults to the
# same, so a bwrap that did not say otherwise would leave the payload
# and the daemons as the same uid and no rule could tell them apart:
# mitm's own outbound to :443 would be dnat'd back to mitm and the
# run would hang.
#
# So the payload runs --uid 1 --gid 1, which lands outside bwrap #2's
# uid_map and is seen as the overflow uid through the nesting, and
# the dnat rule keys on the difference:
#
#     meta skuid != 0 tcp dport { 80, 443 } dnat to 127.0.0.1:8080
#
# The exemption is kernel-enforced by identity and not by excepting
# mitm's traffic: no mark, no cgroup, no SNI filter, and no
# configuration the payload can reach makes mitm's own dials look
# redirected.  The payload cannot see the daemons (--unshare-pid)
# and cannot signal them.
#
# TRUST STORE
#
# The per-run CA is an EC P-256 key, generated in well under ten
# milliseconds -- RSA 2048 would be 100-300 ms and RSA 4096 seconds,
# either visible at the flag.  Prepare writes ca-certificates.crt =
# the host bundle plus this run's CA, and bwrap #1 binds the run
# directory read-only over /etc/ssl; because a bind of a file wins
# over a bind of its parent directory, the rewritten bundle is what
# every reader sees.  The payload's own sandbox --share-net re-binds
# the view it inherited, so sandbox.sh is not modified and no caller
# of netsandbox needs to know.
#
# CLI
#
# Same shape as sandbox: leading --envs=NAME / NAME=value, then bwrap
# args, then -- and the command.  Our own flags occupy --net-*, which
# bwrap does not use; the first -- ends them.
#
# WHAT IS NOT DONE YET
#
# - The mitm addon is a placeholder: it logs the request line and
#   passes everything through.  Commit two replaces it with the host
#   check and the --net-method filter.
# - --net-log is accepted and will be handed to the addon once the
#   addon exists; no format has been chosen.
# - IPv6 is absent from the netns and not provided for.
# - The uid the payload lands at under the bwrap nesting is the piece
#   most likely to need adjustment; see THE UID EXEMPTION.

[[ -n ${_DIC_LOADED:-} ]] || source "${BASH_SOURCE[0]%/*}/dic.sh"

# Absolute, so a cd inside the inner shell leaves the path valid.
NETSANDBOX_SCRIPTS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || {
    dic-error 'netsandbox-error: cannot resolve the scripts directory'
    return 1
}

# ---------------------------------------------------------------- help

# The help text, on stdout.  It is written by hand and not generated,
# because the useful thing to put beside each --net-* flag is the
# security posture that flag selects -- the whole reason the wrapper
# exists, and the part a `bwrap --help` heading cannot tell you.  A
# malformed --net-* flag prints this to stderr, so the caller sees
# the flag they meant beside the flag we did not know.
netsandbox-help() {
    cat <<'EOF'
usage: netsandbox [--envs=NAME ...] [--net-* FLAG ...] [--] BWRAP_ARGS... COMMAND [ARGS...]

A sandbox wrapper with fine-grained network control.  With no --net-*
flag it is sandbox: --unshare-net and no route off the machine.  A
--net-* flag builds a private network namespace whose only route off
the machine is a controlled egress.

Network modes (choose one):

  (no flag)               --unshare-net; no route off the machine.

  --net-allow-hosts H,H   A dnsmasq in the jail is the only resolver the
                          payload can reach.  One --server rule per
                          allowlisted name and a --local=/#/ catch-all,
                          so a query for a name not on the list gets
                          NXDOMAIN instead of being forwarded.  Once a
                          name resolves, any protocol to that address
                          passes.  Comma-separated; a leading dot is a
                          dnsmasq suffix rule, so .example.com covers
                          example.com and every name under it.

  --net-mitm              HTTP(S)-only.  dnat's the payload's :80 and
                          :443 to a per-run mitmproxy that filters by
                          the Host header and TLS SNI.  A non-HTTP
                          stream is refused.  Requires --net-allow-hosts
                          to have anything to reach.

  --net-method M,M        Allow only these HTTP methods.  A method is
                          inside the TLS record, so this requires
                          --net-mitm; a silent downgrade to SNI-only is
                          refused at startup.

  --net-deny-method M,M   Deny these HTTP methods.  Requires --net-mitm.

  --net-log FILE          Write one JSONL line per request.  Requires
                          --net-mitm.

  --net-raw               --share-net and no jail.  The escape hatch,
                          refused alongside any other --net-* flag.

--envs=NAME and leading NAME=value are sandbox's prefix; netsandbox
does not yet support them.  Any other argument that begins with - is a
bwrap argument, and the first -- ends the option run.  Everything
after -- is the command, verbatim.

Exit codes:
  0   the command ran (whatever it returned)
  1   the jail could not be built
  64  usage error
EOF
}

# -------------------------------------------------------------------- deps

# Resolve a binary, preferring PATH but falling back to the sbin
# directories Debian puts nft and dnsmasq in, which are not always on
# a user's PATH.
netsandbox-bin() {
    local name=$1 fallback
    command -v "$name" >/dev/null 2>&1 && { command -v "$name"; return 0; }
    for fallback in /sbin/$name /usr/sbin/$name /usr/bin/$name; do
        [[ -x $fallback ]] && { printf '%s\n' "$fallback"; return 0; }
    done
    return 1
}

# Locate pasta.  slirp4netns does the same work, but it takes a PID
# and configures the netns from outside, so it cannot launch the
# inner script itself: a caller would have to hold a process open in
# the target namespaces and nsenter into it.  pasta creates the
# namespaces and runs the command in them, which is the shape the
# rest of this file builds on.
netsandbox-egress-helper() {
    local bin
    bin=$(netsandbox-bin pasta) && { printf '%s\n' "$bin"; return 0; }
    return 1
}

# ---------------------------------------------------------------- validate

netsandbox-validate() {
    local missing=() need h
    for need in unshare bash awk grep openssl ip; do
        command -v "$need" >/dev/null 2>&1 || missing+=("$need")
    done
    for need in nft dnsmasq bwrap; do
        netsandbox-bin "$need" >/dev/null || missing+=("$need")
    done
    netsandbox-egress-helper >/dev/null || missing+=('pasta')
    if (( ${#missing[@]} )); then
        dic-error "netsandbox-error: not installed: ${missing[*]}"
        dic-hint  'netsandbox-hint: on Debian: apt install nftables dnsmasq-base passt bubblewrap'
        return 1
    fi
    if ! unshare -rn true 2>/dev/null; then
        dic-error 'netsandbox-error: unprivileged user namespaces are disabled'
        dic-hint  'netsandbox-hint: sysctl -w kernel.unprivileged_userns_clone=1'
        return 1
    fi

    # Mode consistency.  A method inside the TLS record needs an
    # interceptor, so --net-method without --net-mitm is a startup
    # error and never a silent downgrade to SNI-only.
    if (( net_raw )) && { [[ -n $net_allow ]] || (( net_mitm )) \
                          || [[ -n $net_method_allow || -n $net_method_deny ]]; }; then
        dic-error 'netsandbox-error: --net-raw contradicts the other --net-* flags'
        return 1
    fi
    if [[ -n $net_method_allow || -n $net_method_deny ]] && (( ! net_mitm )); then
        dic-error 'netsandbox-error: --net-method needs --net-mitm'
        dic-hint  'netsandbox-hint: a method is inside the TLS record, so filtering one requires interception'
        return 1
    fi

    # dnsmasq matches --server domains by suffix and does not know a
    # literal *; a rule it cannot match is an empty allowlist and not
    # an error, so a wildcard is refused here rather than passed
    # through.
    for h in $net_allow; do
        if [[ $h == *'*'* ]]; then
            dic-error "netsandbox-error: --net-allow-hosts $h: '*' is not dnsmasq syntax"
            dic-hint  'netsandbox-hint: a leading dot is a suffix rule, e.g. .example.com'
            return 1
        fi
    done

    if (( net_mitm )); then
        netsandbox-bin mitmdump  >/dev/null 2>&1 \
            || netsandbox-bin mitmproxy >/dev/null 2>&1 || {
            dic-error 'netsandbox-error: mitmproxy is not installed'
            dic-hint  'netsandbox-hint: pip install mitmproxy'
            return 1
        }
    fi

    [[ -r /etc/ssl/certs/ca-certificates.crt ]] || {
        dic-error 'netsandbox-error: /etc/ssl/certs/ca-certificates.crt is not readable'
        return 1
    }
    return 0
}

# ----------------------------------------------------------------- prepare

# Scratch for this run: the generated CA, the merged bundle, the
# dnsmasq config, the daemons script and the addon.  Removed on every
# return, so a failed run leaves nothing for a later one to trip over.
netsandbox-run-dir() { printf '%s\n' "${NETSANDBOX_RUNDIR:?}"; }

# The dnsmasq config, on stdout.  One --server rule per allowlisted
# name, and --local=/#/ as the last rule: a query for a name that
# matches no --server falls through to the catch-all and is answered
# NXDOMAIN rather than forwarded.  The list is written verbatim, so a
# leading dot is dnsmasq's suffix rule and a bare name is exact.
netsandbox-dnsmasq-conf() {
    local upstream=$1 h
    for h in $net_allow; do
        printf 'server=/%s/%s\n' "$h" "$upstream"
    done
    printf 'local=/#/\n'
}

# Generate the per-run CA.  EC P-256, so the cost is invisible at the
# flag.  Writes the key, the cert and the hash symlink some tools
# read instead of the bundle.
netsandbox-make-ca() {
    local dir=$1 key=$1/mitm-ca.key crt=$1/mitm-ca.crt hash
    openssl ecparam -name prime256v1 -genkey -noout -out "$key" 2>/dev/null || return 1
    openssl req -new -x509 -key "$key" -out "$crt" -days 1 \
        -subj '/CN=netsandbox local CA' 2>/dev/null || return 1
    hash=$(openssl x509 -in "$crt" -noout -hash 2>/dev/null) || return 1
    ln -sf "$crt" "$dir/$hash.0" || return 1
    return 0
}

# The bundle the payload's trust store becomes: the host's bundle
# with this run's CA appended.
netsandbox-make-bundle() {
    cat /etc/ssl/certs/ca-certificates.crt "$1/mitm-ca.crt" \
        > "$1/ca-certificates.crt"
}

netsandbox-prepare() {
    local dir upstream
    dir=$(netsandbox-run-dir)
    netsandbox-make-ca "$dir" || {
        dic-error 'netsandbox-error: could not generate the run CA'
        return 1
    }
    netsandbox-make-bundle "$dir" || {
        dic-error 'netsandbox-error: could not build the trust bundle'
        return 1
    }

    # The upstream dnsmasq forwards allowlisted names to.  Read
    # outside the netns, because the host's resolvers are not
    # reachable directly from inside; the packet goes through pasta.
    upstream=$(awk '/^[[:space:]]*nameserver/ { print $2; exit }' /etc/resolv.conf)
    [[ -n $upstream ]] || {
        dic-error 'netsandbox-error: no nameserver in /etc/resolv.conf'
        return 1
    }
    NETSANDBOX_UPSTREAM=$upstream

    netsandbox-dnsmasq-conf "$upstream" > "$dir/dnsmasq.conf" || return 1

    # resolv.conf for inside the netns: only dnsmasq, so the payload's
    # resolver is dnsmasq by construction whatever it is.
    printf 'nameserver 127.0.0.1\n' > "$dir/resolv.conf" || return 1
    return 0
}

# ---------------------------------------------------------------- daemons

# The script bwrap #1 runs: dnsmasq, then mitm if asked, then the
# payload's own sandbox.  Written to a file and not passed as an -c
# argument, because a page of shell does not survive the quoting of
# two bwrap layers.
netsandbox-write-daemons() {
    local file=$1
    {
        cat <<'PRE'
set -e

source "$NETSANDBOX_SCRIPTS_DIR/sandbox.sh"

"$NETSANDBOX_DNSMASQ" \
    --no-daemon --keep-in-foreground \
    --user=root \
    --port=53 --bind-interfaces --listen-address=127.0.0.1 \
    --no-resolv --no-hosts \
    --conf-file="$NETSANDBOX_RUNDIR/dnsmasq.conf" &
DNSMASQ_PID=$!
trap 'kill "$DNSMASQ_PID" 2>/dev/null || true' EXIT

for _ in 1 2 3 4 5 6 7 8 9 10; do
    (exec 3<>/dev/udp/127.0.0.1/53) 2>/dev/null && { exec 3>&-; break; }
    sleep 0.05
done
PRE
        if (( net_mitm )); then
            cat <<'MITM'

# mitmproxy in transparent mode.  The CA generated outside is handed
# over with --set confdir, so the proxy does not write one of its own
# and the per-run cost stays the EC key's.
"$NETSANDBOX_MITM" \
    --mode transparent \
    --listen-host 127.0.0.1 --listen-port 8080 \
    --set confdir="$NETSANDBOX_RUNDIR" \
    --set stream_large_bodies=1m \
    -s "$NETSANDBOX_RUNDIR/addon.py" &
MITM_PID=$!
trap 'kill "$DNSMASQ_PID" "$MITM_PID" 2>/dev/null || true' EXIT

for _ in 1 2 3 4 5 6 7 8 9 10; do
    (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null && { exec 3>&-; break; }
    sleep 0.05
done
MITM
        fi
        cat <<'TAIL'

# The payload's own sandbox, --share-net so it shares this netns (to
# reach dnsmasq on :53 and mitm on :8080), --uid 1 --gid 1 so it is
# a different uid from the daemons and the dnat can tell them apart.
# The trust store was rewritten by bwrap #1, so sandbox's own
# --ro-bind /etc/ssl re-binds the modified view it inherited.
sandbox --share-net --uid 1 --gid 1 "$@"
TAIL
    } > "$file"
}

# The placeholder addon.  Commit two replaces this with the host and
# method filter; for now it logs the request line and lets everything
# through, so a run that must pass does and the transcript shape is
# visible.
netsandbox-write-addon() {
    cat > "$1" <<'ADDON'
from mitmproxy import http

class NetSandbox:
    def request(self, flow: http.HTTPFlow) -> None:
        print("netsandbox-mitm: %s %s" % (flow.request.method, flow.request.pretty_host))

addons = [NetSandbox()]
ADDON
}

# ------------------------------------------------------------------- inner

# The script run inside `unshare -rmn`: loopback up, IPv6 off, pasta
# as the only route off, nft, then bwrap #1.
netsandbox-write-inner() {
    cat > "$1" <<INNER
set -e

ip link set lo up
# Egress is already in place: the caller of this script is pasta,
# which created these namespaces and configured the tap interface
# from the host's default route before exec'ing this script.  There
# is nothing here to start, and no helper to look for: pasta that
# was told to run inside a netns it did not create falls into local
# mode, which is where the uid_map write failed.
done

# --- nft ---
# Policy drop on the output hook, and four rules that say what may
# leave: loopback (so dnsmasq on 127.0.0.1 is reachable), the
# upstream on :53 (so dnsmasq can forward), the daemons to anywhere
# (mitm's own outbound, and dnsmasq's), and everything else -- the
# payload, whose uid is not 0 -- dnat'd to mitm on :80 and :443.
#
# The chain is natout and not dnat: dnat is a statement keyword in
# nft's lexer, and a bare word is matched against the keywords
# first, so a chain named dnat parses as the start of a statement
# and the line is a syntax error at the name.
#
# The heredoc is unquoted, so \$NETSANDBOX_UPSTREAM -- which the
# outer heredoc left as a literal \$ -- is expanded by the shell
# that reads this file, where NETSANDBOX_UPSTREAM is in the
# environment.
"\$NETSANDBOX_NFT" -f - <<NFT
add table inet netfilter
add chain inet netfilter natout { type nat hook output priority -100; }
add chain inet netfilter output { type filter hook output priority 0; policy drop; }
add rule inet netfilter output oifname "lo" accept
add rule inet netfilter output ip daddr \$NETSANDBOX_UPSTREAM udp dport 53 accept
add rule inet netfilter output ip daddr \$NETSANDBOX_UPSTREAM tcp dport 53 accept
add rule inet netfilter output meta skuid 0 accept
add rule inet netfilter natout meta skuid != 0 tcp dport { 80, 443 } dnat ip to 127.0.0.1:8080
NFT

# --- bwrap #1: the daemons ---
# A sandbox.sh-style map and nothing else.  The payload's \$HOME is
# not in this map, and neither is the parent of the CA key's
# directory in the caller's tree; /etc/ssl carries the run CA.
bwrap \\
    --die-with-parent \\
    --new-session \\
    --unshare-user \\
    --unshare-ipc \\
    --unshare-pid \\
    --unshare-uts \\
    --cap-drop ALL \\
    --share-net \\
    --ro-bind /usr /usr \\
    \$( [[ -L /bin  ]] && echo --symlink "\$(readlink /bin)"  /bin  ) \\
    \$( [[ -L /sbin ]] && echo --symlink "\$(readlink /sbin)" /sbin ) \\
    \$( [[ -L /lib  ]] && echo --symlink "\$(readlink /lib)"  /lib  ) \\
    --ro-bind /etc/alternatives     /etc/alternatives \\
    --ro-bind /etc/bash.bashrc      /etc/bash.bashrc \\
    --ro-bind /etc/ca-certificates  /etc/ca-certificates \\
    --ro-bind /etc/group            /etc/group \\
    --ro-bind /etc/hosts            /etc/hosts \\
    --ro-bind /etc/ld.so.cache      /etc/ld.so.cache \\
    --ro-bind /etc/ld.so.conf       /etc/ld.so.conf \\
    --ro-bind /etc/localtime        /etc/localtime \\
    --ro-bind /etc/nsswitch.conf    /etc/nsswitch.conf \\
    --ro-bind /etc/passwd           /etc/passwd \\
    --ro-bind /etc/profile          /etc/profile \\
    --ro-bind /etc/ssl              /etc/ssl \\
    --ro-bind "\$NETSANDBOX_RUNDIR/ca-certificates.crt" /etc/ssl/certs/ca-certificates.crt \\
    --ro-bind "\$NETSANDBOX_RUNDIR/resolv.conf"        /etc/resolv.conf \\
    --ro-bind "\$NETSANDBOX_SCRIPTS_DIR" "\$NETSANDBOX_SCRIPTS_DIR" \\
    --ro-bind "\$NETSANDBOX_RUNDIR"      "\$NETSANDBOX_RUNDIR" \\
    --proc /proc \\
    --dev /dev \\
    --tmpfs /tmp \\
    --tmpfs /run \\
    --clearenv \\
    --setenv PATH "\$PATH" \\
    --setenv HOME /tmp \\
    --setenv NETSANDBOX_SCRIPTS_DIR "\$NETSANDBOX_SCRIPTS_DIR" \\
    --setenv NETSANDBOX_RUNDIR      "\$NETSANDBOX_RUNDIR" \\
    --setenv NETSANDBOX_DNSMASQ     "\$NETSANDBOX_DNSMASQ" \\
    --setenv NETSANDBOX_MITM        "\$NETSANDBOX_MITM" \\
    -- /bin/bash "\$NETSANDBOX_RUNDIR/daemons.sh" "\$@"
INNER
}

# --------------------------------------------------------------------- run

netsandbox-run() {
    local dir helper
    dir=$(mktemp -d "${TMPDIR:-/tmp}/netsandbox.XXXXXXXX") || return 1
    NETSANDBOX_RUNDIR=$dir
    trap 'rm -rf "$NETSANDBOX_RUNDIR"' RETURN

    netsandbox-prepare || return 1

    NETSANDBOX_DNSMASQ=$(netsandbox-bin dnsmasq) || return 1
    NETSANDBOX_NFT=$(netsandbox-bin nft)         || return 1
    if (( net_mitm )); then
        NETSANDBOX_MITM=$(netsandbox-bin mitmdump) \
            || NETSANDBOX_MITM=$(netsandbox-bin mitmproxy) \
            || return 1
    else
        NETSANDBOX_MITM=
    fi

    netsandbox-write-daemons "$dir/daemons.sh" || return 1
    if (( net_mitm )); then
        netsandbox-write-addon "$dir/addon.py" || return 1
    fi

    helper=$(netsandbox-egress-helper) || return 1
    netsandbox-write-inner "$dir/inner.sh" || return 1

    # pasta creates the user and network namespaces, configures the
    # tap interface in the new netns from the host's default route,
    # and runs the inner script in them, so it is pasta and not an
    # unshare above pasta that owns the namespaces.  --no-map-gw
    # is what stops the payload from reaching the host's loopback,
    # and the -t/-u/-T/-U none close the four port forwards pasta
    # opens by default.
    NETSANDBOX_RUNDIR="$dir" \
    NETSANDBOX_UPSTREAM="$NETSANDBOX_UPSTREAM" \
    NETSANDBOX_NFT="$NETSANDBOX_NFT" \
    NETSANDBOX_DNSMASQ="$NETSANDBOX_DNSMASQ" \
    NETSANDBOX_MITM="$NETSANDBOX_MITM" \
    "$helper" --config-net --no-map-gw \
        -t none -u none -T none -U none \
        -- bash "$dir/inner.sh" -- "$@"
    return $?
}

# ------------------------------------------------------------------- entry

netsandbox() {
    # exit codes:
    # 0   the command ran (whatever it returned)
    # 1   the jail could not be built
    # 64  usage error
    #
    # netsandbox's own flags are parsed first; the first -- ends them,
    # and everything after is the caller's, verbatim.  A flag that is
    # not one of ours is passed through, per scripts/AGENTS.md, so a
    # bwrap flag added upstream works here unedited.
    local net_allow= net_mitm=0 net_raw=0 net_log=
    local net_method_allow= net_method_deny=
    local -a rest=() names=()
    local list name

    case ${1:-} in
        --help|-h)
            netsandbox-help
            return 0 ;;
    esac

    # Leading --envs=NAME / NAME=value, peeled the way sandbox peels
    # them, so the two wrappers agree on where the run of them ends
    # and the caller can type the same prefix beside either.
    while (( $# )); do
        if [[ $1 == *=* && ${1%%=*} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            dic-error 'netsandbox-error: NAME=value is not supported yet'
            return 64
        fi
        case $1 in
            --envs=*|--envs)
                dic-error 'netsandbox-error: --envs is not supported yet'
                return 64 ;;
            *) break ;;
        esac
    done

    while (( $# )); do
        case $1 in
            --net-allow-hosts|--net-allow-hosts=*)
                case $1 in *=*) list=${1#*=}; shift ;; *) list=$2; shift 2 ;; esac
                [[ -n $list ]] || { dic-error 'netsandbox-error: --net-allow-hosts needs a value'; return 64; }
                IFS=, read -ra names <<< "$list"
                for name in "${names[@]}"; do
                    [[ -n $name ]] && net_allow+="${net_allow:+,}$name"
                done ;;
            --net-mitm)     net_mitm=1; shift ;;
            --net-method|--net-method=*)
                case $1 in *=*) list=${1#*=}; shift ;; *) list=$2; shift 2 ;; esac
                net_method_allow+="${net_method_allow:+,}$list" ;;
            --net-deny-method|--net-deny-method=*)
                case $1 in *=*) list=${1#*=}; shift ;; *) list=$2; shift 2 ;; esac
                net_method_deny+="${net_method_deny:+,}$list" ;;
            --net-log|--net-log=*)
                case $1 in *=*) net_log=${1#*=}; shift ;; *) net_log=$2; shift 2 ;; esac ;;
            --net-raw)      net_raw=1; shift ;;
            --)             shift; rest+=("$@"); break ;;
            *)              rest+=("$1"); shift ;;
        esac
    done

    netsandbox-validate || return $?

    # No network flag at all, or the raw escape hatch, are the two
    # cases that do not build a netns: they are sandbox, unchanged,
    # with the caller's own words.
    if [[ -z $net_allow && $net_mitm -eq 0 && $net_raw -eq 0 ]]; then
        sandbox "${rest[@]}"
        return $?
    fi
    if (( net_raw )); then
        sandbox --share-net "${rest[@]}"
        return $?
    fi

    netsandbox-run "${rest[@]}"
}

# A test that sources this file gets the functions; a shell that runs
# it gets the command.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    netsandbox "$@"
fi