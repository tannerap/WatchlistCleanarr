"""Webhook API key authentication."""

from __future__ import annotations

import os

from flask import Request


def get_expected_api_key() -> str | None:
    key = os.environ.get("WEBHOOK_API_KEY", "").strip()
    return key or None


def extract_provided_api_key(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None

    for header_name in ("X-API-Key", "X-Webhook-Token"):
        value = request.headers.get(header_name)
        if value:
            return value.strip()

    query_value = request.args.get("apikey")
    if query_value:
        return query_value.strip()

    return None


def is_authorized(request: Request) -> bool:
    expected = get_expected_api_key()
    if not expected:
        return True
    provided = extract_provided_api_key(request)
    return provided == expected
