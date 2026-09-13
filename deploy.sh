#!/bin/sh
# One-command deploy: ./deploy.sh "what changed"
cd "$(dirname "$0")" && git add -A && git commit -m "${1:-update}" && git push
