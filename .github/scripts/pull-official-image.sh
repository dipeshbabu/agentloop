#!/usr/bin/env bash
# Pull a Docker Official Image from the first mirror that serves it.
#
# Usage: pull-official-image.sh NAME:TAG [LOCAL_TAG]
#
# ECR Public can throttle hosted runners (429, "Data limit exceeded") for
# minutes, so each round tries the same official image from ECR Public,
# Google's Docker Hub mirror and Docker Hub before backing off. The pulled
# reference is printed on stdout; LOCAL_TAG, when given, also names it.
set -euo pipefail

image="${1:?usage: pull-official-image.sh NAME:TAG [LOCAL_TAG]}"
local_tag="${2:-}"
registries=(public.ecr.aws/docker/library mirror.gcr.io/library docker.io/library)

for delay in 30 90 0; do
  for registry in "${registries[@]}"; do
    ref="$registry/$image"
    if docker pull --quiet "$ref" >&2; then
      if [ -n "$local_tag" ]; then
        docker tag "$ref" "$local_tag"
      fi
      echo "$ref"
      exit 0
    fi
    echo "::warning::Pull of $ref failed" >&2
  done
  if [ "$delay" -eq 0 ]; then
    echo "::error::Could not pull $image from any mirror" >&2
    exit 1
  fi
  echo "::warning::Retrying $image in ${delay}s" >&2
  sleep "$delay"
done
