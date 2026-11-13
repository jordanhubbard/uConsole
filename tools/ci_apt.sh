#!/usr/bin/env bash
# Install CI tools using temporary copies of the runner's signed APT sources.
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true ||
      "${RUNNER_ENVIRONMENT:-}" != github-hosted ||
      "$(uname -s)" != Linux ]]; then
    echo "APT bootstrap requires a GitHub-hosted Linux job" >&2
    exit 64
fi
if [[ $# -eq 0 ]]; then
    echo "usage: ci_apt.sh [apt install options] package ..." >&2
    exit 64
fi
fallback=
case "$(dpkg --print-architecture)" in
    amd64|i386)
        archive=https://archive.ubuntu.com/ubuntu
        security=https://security.ubuntu.com/ubuntu
        fallback=https://mirrors.kernel.org/ubuntu
        ;;
    arm64|armhf|ppc64el|riscv64|s390x)
        archive=https://ports.ubuntu.com/ubuntu-ports
        security=$archive
        ;;
    *)
        echo "Unsupported Ubuntu runner architecture" >&2
        exit 64
        ;;
esac
source_root=${CI_APT_SOURCE_ROOT:-/etc/apt}
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
normalize() {
    sed -e "s#mirror+file:/etc/apt/apt-mirrors\.txt#$archive#g" \
        -e "s#mirror+file:/etc/apt/apt-mirrors-security\.txt#$security#g" \
        -e "s#http://azure\.archive\.ubuntu\.com/ubuntu#$archive#g" \
        -e "s#https://azure\.archive\.ubuntu\.com/ubuntu#$archive#g" \
        -e "s#http://archive\.ubuntu\.com/ubuntu#$archive#g" \
        -e "s#https://archive\.ubuntu\.com/ubuntu#$archive#g" \
        -e "s#http://security\.ubuntu\.com/ubuntu#$security#g" \
        -e "s#https://security\.ubuntu\.com/ubuntu#$security#g" "$1"
}
prepare_sources() {
    mkdir -p "$temporary/sources.list.d"
    : >"$temporary/sources.list"
    local found=0 source
    if [[ -f "$source_root/sources.list" ]]; then
        normalize "$source_root/sources.list" >"$temporary/sources.list"
        found=1
    fi
    for source in "$source_root/sources.list.d/"*.list "$source_root/sources.list.d/"*.sources; do
        [[ -f "$source" ]] || continue
        normalize "$source" >"$temporary/sources.list.d/$(basename "$source")"
        found=1
    done
    if [[ $found -eq 0 ]]; then
        echo "No APT source configuration found" >&2
        return 64
    fi
}
prepare_sources
options=(-o "Dir::Etc::sourcelist=$temporary/sources.list"
         -o "Dir::Etc::sourceparts=$temporary/sources.list.d")
bootstrap() {
    sudo apt-get "${options[@]}" -o APT::Update::Error-Mode=any update -q || return $?
    sudo apt-get "${options[@]}" install -y -q "$@"
}
if bootstrap "$@" >"$temporary/primary.log" 2>&1; then
    cat "$temporary/primary.log"
    exit 0
else
    result=$?
    cat "$temporary/primary.log"
fi
# Retry only the observed Ubuntu DNS failure. Signature, expiry and package
# errors remain failures. The mirror uses the same signed Ubuntu metadata/keys.
if [[ -n "$fallback" ]] &&
   grep -Eq "Temporary failure resolving '(azure[.]archive[.]ubuntu[.]com|archive[.]ubuntu[.]com|security[.]ubuntu[.]com)'" "$temporary/primary.log" &&
   ! grep -Eiq "NO_PUBKEY|BADSIG|EXPKEYSIG|NOSPLIT|GPG error:|not signed|Release file.*(expired|not valid yet)" "$temporary/primary.log"; then
    echo "Ubuntu DNS failed; retrying the signed archive on mirrors.kernel.org" >&2
    archive=$fallback
    security=$fallback
    prepare_sources
    if bootstrap "$@" >"$temporary/fallback.log" 2>&1; then
        cat "$temporary/fallback.log"
        exit 0
    else
        result=$?
        cat "$temporary/fallback.log"
    fi
fi
exit "$result"
