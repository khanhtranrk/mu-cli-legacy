"""Authenticated MasterFlow HTTP transport. Mutating calls are never retried."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from mu.spec_yaml import load_spec_yaml

from mu.http_headers import request_headers


class MasterFlowClient:
    def __init__(self, server: str, token: str):
        parsed = urlsplit(server)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("server must be an HTTP site URL without credentials, query or fragment")
        self.server = server.rstrip("/")
        self.token = token

    def _call(self, path, body=None):
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/yaml" if body is None else "application/json"}
        if body is None:
            headers["X-Mu-Expression-Engines"] = "cel-v1"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = Request(self.server + path, data=data, headers=request_headers(headers))
        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            # Do not echo response bodies: they may contain credentials or user payloads.
            raise ValueError(f"MasterFlow {path}: HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ValueError(f"MasterFlow {path}: connection failed; request was not retried") from None

    def post(self, path, body):
        return json.loads(self._call(path, body))

    def spec(self, path):
        value = load_spec_yaml(self._call(path))
        if not isinstance(value, dict):
            raise ValueError("remote spec root must be a mapping")
        return value
