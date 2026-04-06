#!/usr/bin/env python3

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def request_json(url: str, token: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "voice-tracker-release",
    }
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def delete(url: str, token: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "voice-tracker-release",
    }
    request = urllib.request.Request(url, headers=headers, method="DELETE")
    with urllib.request.urlopen(request, timeout=30):
        return


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    sha = os.environ["GITHUB_SHA"]

    owner, service_repo = repo.split("/", 1)
    package_root = urllib.parse.quote(service_repo, safe="")

    for service in ("gateway", "tracker", "writer", "commands"):
        package = urllib.parse.quote(f"{service_repo}/{service}", safe="")
        url = f"https://api.github.com/users/{owner}/packages/container/{package}/versions?per_page=100"

        try:
            versions = request_json(url, token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise

        for version in versions:
            tags = version.get("metadata", {}).get("container", {}).get("tags", [])
            if sha not in tags:
                continue

            version_id = version["id"]
            delete_url = f"https://api.github.com/users/{owner}/packages/container/{package}/versions/{version_id}"
            try:
                delete(delete_url, token)
                print(f"deleted partial tag for {service}:{sha}")
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
