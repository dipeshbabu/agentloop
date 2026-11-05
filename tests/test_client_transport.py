from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from agentloop.client import AgentLoopClient, AgentLoopClientError


@pytest.fixture
def endpoint():
    servers = []

    def start(*, status=200, body=b"{}", location=None, requests=None):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if requests is not None:
                    requests.append(dict(self.headers))
                self.send_response(status)
                if location is not None:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.do_GET()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("scope", ["project", "admin"])
def test_credentials_are_never_forwarded_to_a_redirect_target(endpoint, status, scope):
    captured = []
    target = endpoint(requests=captured)
    origin = endpoint(status=status, location=target + "/capture")
    client = AgentLoopClient(
        base_url=origin, api_key="synthetic-project", admin_api_key="synthetic-admin"
    )
    with pytest.raises(AgentLoopClientError, match=f"HTTP {status}"):
        if scope == "admin":
            client.create_api_key()
        else:
            client.health()
    assert captured == []


def test_error_responses_do_not_copy_private_payloads_into_exceptions(endpoint):
    client = AgentLoopClient(base_url=endpoint(status=401, body=b"SYNTHETIC_PRIVATE_RESPONSE"))
    with pytest.raises(AgentLoopClientError, match="HTTP 401") as caught:
        client.health()
    assert "SYNTHETIC_PRIVATE_RESPONSE" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_client_repr_omits_project_and_admin_keys():
    client = AgentLoopClient(api_key="synthetic-project", admin_api_key="synthetic-admin")
    assert "synthetic-project" not in repr(client)
    assert "synthetic-admin" not in repr(client)


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b"\xff"])
def test_invalid_responses_raise_the_client_error(endpoint, body):
    with pytest.raises(AgentLoopClientError, match="response"):
        AgentLoopClient(base_url=endpoint(body=body)).health()


def test_response_reads_have_a_configurable_size_limit(endpoint):
    client = AgentLoopClient(
        base_url=endpoint(body=b'{"data":"' + b"x" * 100 + b'"}'), max_response_bytes=16
    )
    with pytest.raises(AgentLoopClientError, match="size limit"):
        client.health()


def test_empty_success_response_is_supported(endpoint):
    assert AgentLoopClient(base_url=endpoint(status=204, body=b"")).health() == {}


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeout_is_rejected_before_network_access(value):
    with pytest.raises(AgentLoopClientError, match="timeout_s"):
        AgentLoopClient(timeout_s=value).health()


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_response_limit_is_rejected_before_network_access(value):
    with pytest.raises(AgentLoopClientError, match="max_response_bytes"):
        AgentLoopClient(max_response_bytes=value).health()


@pytest.mark.parametrize(
    "url",
    [
        "https://user:synthetic-secret@example.test",
        "https://example.test?secret=1",
        "https://example.test/#secret",
        "http://example.test:invalid",
    ],
)
def test_ambiguous_urls_are_rejected_before_network_access(url):
    with pytest.raises(AgentLoopClientError, match="API URL"):
        AgentLoopClient(base_url=url).health()


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
def test_v1_suffix_is_normalized(endpoint, suffix):
    assert AgentLoopClient(base_url=endpoint() + suffix).health() == {}
