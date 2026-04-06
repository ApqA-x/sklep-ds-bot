#!/usr/bin/env python3

import json
import os
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    token = os.environ["GITHUB_TOKEN"]
    url = f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&per_page=100"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "voice-tracker-release",
    }

    for _ in range(60):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except urllib.error.URLError as exc:
            print(f"waiting for CI: {exc}")
            time.sleep(10)
            continue

        for run in payload.get("workflow_runs", []):
            if run.get("name") != "CI":
                continue
            if run.get("event") != "push":
                continue
            if run.get("head_branch") != "main":
                continue
            head_repo = run.get("head_repository") or {}
            if head_repo.get("full_name") != repo:
                continue

            status = run.get("status")
            conclusion = run.get("conclusion")

            if status != "completed":
                break

            if conclusion == "success":
                return 0

            print(f"CI finished with conclusion={conclusion}")
            return 1

        time.sleep(10)

    print("Timed out waiting for CI to complete successfully")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
