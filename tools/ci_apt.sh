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
case "$(dpkg --print-architecture)" in
    amd64|i386)
        archive=https://archive.ubuntu.com/ubuntu
        security=https://security.ubuntu.com/ubuntu
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
mkdir "$temporary/sources.list.d"
normalize() {
    sed -e "s#mirror+file:/etc/apt/apt-mirrors\.txt#$archive#g" \
        -e "s#mirror+file:/etc/apt/apt-mirrors-security\.txt#$security#g" \
        -e "s#http://azure\.archive\.ubuntu\.com/ubuntu#$archive#g" \
        -e "s#https://azure\.archive\.ubuntu\.com/ubuntu#$archive#g" "$1"
}
: >"$temporary/sources.list"
found=0
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
    exit 64
fi
options=(-o "Dir::Etc::sourcelist=$temporary/sources.list"
         -o "Dir::Etc::sourceparts=$temporary/sources.list.d")
sudo apt-get "${options[@]}" -o APT::Update::Error-Mode=any update -q
sudo apt-get "${options[@]}" install -y -q "$@"
