#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <public-domain-or-ip>" >&2
  exit 2
fi

target=$1
nmap -Pn -sT -p 1-65535 --open --reason "$target"

echo "Review from an external network: only tcp/80 and tcp/443 may be open."
