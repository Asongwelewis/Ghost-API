"""Action self-test fixture (ci.yml): one hallucinated call, one valid unregistered call."""

import requests


def fetch():
    return requests.gett("https://example.com")


def send():
    return requests.post("https://example.com", json={})
