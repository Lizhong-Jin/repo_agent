"""Public HTTP GET with DNS pinning, pre-request peer checks and bounded bodies."""

import asyncio
import ipaddress
import re
import socket
import zlib
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from .web_errors import WebError

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_URL_CHARS = 2048
MAX_REDIRECTS = 5
# Supplement older Python releases whose special-purpose address tables differ.
_FORBIDDEN_V4 = tuple(ipaddress.ip_network(value) for value in ("192.0.0.0/24", "192.88.99.0/24"))
_FORBIDDEN_V6 = tuple(
    ipaddress.ip_network(value)
    for value in (
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2001::/32",
        "2001::/23",
        "2002::/16",
        "3fff::/20",
    )
)


def blocked_url():
    return WebError(
        "BLOCKED_URL", "Only unambiguous public HTTP(S) URLs on ports 80/443 are allowed."
    )


def public_ip(value: str):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise blocked_url() from None
    if (
        "%" in value
        or not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ):
        raise blocked_url()
    if address.version == 4 and any(address in network for network in _FORBIDDEN_V4):
        raise blocked_url()
    if address.version == 6 and (
        address.is_site_local or any(address in network for network in _FORBIDDEN_V6)
    ):
        raise blocked_url()
    # Azure's platform virtual IP is not classified as private by ipaddress.
    if str(address) == "168.63.129.16":
        raise blocked_url()
    return address


def _hostname(value: str) -> str:
    try:
        literal = ipaddress.ip_address(value)
    except ValueError:
        try:
            host = value.removesuffix(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise blocked_url() from None
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or labels[-1].isdigit()
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels)
            or host.endswith(
                (".localhost", ".local", ".internal", ".home.arpa", ".invalid", ".test", ".onion")
            )
        ):
            raise blocked_url() from None
        return host
    return str(public_ip(str(literal)))


def public_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_URL_CHARS
        or "\\" in value
        or any(c.isspace() or not c.isprintable() for c in value)
    ):
        raise blocked_url()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "%" in parsed.netloc
            or parsed.port not in {None, 80, 443}
        ):
            raise blocked_url()
        host = _hostname(parsed.hostname)
        url = httpx.URL(value)
        # Reject disagreements between URL parsers before passing data to HTTP Core.
        if _hostname(url.host) != host or url.scheme != parsed.scheme:
            raise blocked_url()
        canonical = str(url.copy_with(host=host, fragment=None))
        if len(canonical) > MAX_URL_CHARS:
            raise blocked_url()
        return canonical
    except (ValueError, httpx.InvalidURL):
        raise blocked_url() from None


async def resolve_addresses(host: str, port: int) -> list[str]:
    entries = await asyncio.get_running_loop().getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return list(dict.fromkeys(entry[4][0] for entry in entries))


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Validate all DNS answers, connect to an IP literal, then verify its peer.

    HTTP Core retains the original hostname for Host, SNI and certificate checks.
    Resolver/connector injection is only for trusted application code and tests.
    """

    def __init__(self, *, resolver=resolve_addresses, connector=None):
        self.resolver = resolver
        self.connector = connector if connector is not None else httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port not in {80, 443} or local_address is not None:
            raise blocked_url()
        host = _hostname(host)
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            addresses = await self.resolver(host, port)
        if not addresses or len(addresses) > 64:
            raise blocked_url()
        # Fail closed for mixed public/private DNS answers, including mapped IPv6.
        approved = list(dict.fromkeys(str(public_ip(address)) for address in addresses))
        for index, address in enumerate(approved):
            try:
                stream = await self.connector.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if index == len(approved) - 1:
                    raise
                continue
            try:
                peer = stream.get_extra_info("server_addr")
                if (
                    not isinstance(peer, tuple)
                    or len(peer) < 2
                    or peer[1] != port
                    or public_ip(peer[0]) != ipaddress.ip_address(address)
                ):
                    raise blocked_url()
            except BaseException:
                await stream.aclose()
                raise
            return stream
        raise blocked_url()


async def read_body(chunks, headers, *, limit=MAX_RESPONSE_BYTES) -> bytes:
    encoding = headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip"}:
        raise WebError(
            "UNSUPPORTED_CONTENT_ENCODING", "Only identity and gzip encoding are supported."
        )
    declared = headers.get("content-length", "")
    if declared.isdecimal() and (len(declared) > 10 or int(declared) > limit):
        raise WebError("RESPONSE_TOO_LARGE", "Response exceeds the size limit.")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    body = bytearray()
    received = 0
    try:
        async for chunk in chunks:
            received += len(chunk)
            if received > limit:
                raise WebError("RESPONSE_TOO_LARGE", "Response exceeds the size limit.")
            body.extend(decoder.decompress(chunk, limit - len(body) + 1) if decoder else chunk)
            if len(body) > limit:
                raise WebError("RESPONSE_TOO_LARGE", "Response exceeds the size limit.")
            # Ensure buffered/chunked responses cannot starve cancellation and sibling requests.
            await asyncio.sleep(0)
        if decoder and (not decoder.eof or decoder.unused_data):
            raise ValueError
        return bytes(body)
    except (ValueError, zlib.error):
        raise WebError("INVALID_RESPONSE", "Invalid or incomplete compressed response.") from None


class PublicHTTP:
    def __init__(self, *, network_backend_factory=PublicNetworkBackend):
        self.network_backend_factory = network_backend_factory

    async def download(self, requested_url: str) -> dict:
        current = public_url(requested_url)
        visited = set()
        for hop in range(MAX_REDIRECTS + 1):
            if current in visited:
                raise WebError("REDIRECT_LIMIT", "Redirect loop or too many redirects.")
            visited.add(current)
            # New pool per hop: no cookies, auth, proxy, reused connections or implicit redirects.
            async with httpcore.AsyncConnectionPool(
                network_backend=self.network_backend_factory(),
                max_connections=1,
                max_keepalive_connections=0,
                retries=0,
            ) as pool:
                async with pool.stream(
                    "GET",
                    current,
                    headers={
                        "Accept": "text/html, text/plain, application/json",
                        "Accept-Encoding": "gzip",
                        "User-Agent": "repo-agent-web/0.1",
                    },
                    extensions={"timeout": {"connect": 10, "read": 20, "write": 10, "pool": 20}},
                ) as response:
                    headers = httpx.Headers(response.headers)
                    status = response.status
                    if status in {301, 302, 303, 307, 308}:
                        location = headers.get("location", "")
                        if not location:
                            raise WebError(
                                "HTTP_ERROR", "Redirect has no Location header.", http_status=status
                            )
                        if hop == MAX_REDIRECTS:
                            raise WebError("REDIRECT_LIMIT", "Too many redirects.")
                        # Validate Location before resolving its host or opening another socket.
                        if any(c.isspace() or not c.isprintable() for c in location):
                            raise blocked_url()
                        current = public_url(urljoin(current, location))
                        continue
                    if status == 429:
                        raise WebError(
                            "RATE_LIMITED",
                            "Website rate limit reached.",
                            retryable=True,
                            http_status=status,
                        )
                    if not 200 <= status < 300 or status == 206:
                        raise WebError(
                            "HTTP_ERROR",
                            "Website returned an HTTP error or partial body.",
                            retryable=status >= 500,
                            http_status=status,
                        )
                    media = headers.get("content-type", "").split(";")[0].strip().lower()
                    if media not in {"text/html", "text/plain", "application/json"} and not (
                        media.startswith("application/") and media.endswith("+json")
                    ):
                        raise WebError(
                            "UNSUPPORTED_CONTENT_TYPE",
                            "Only HTML, plain text and JSON are supported.",
                        )
                    body = await read_body(
                        response.aiter_stream(), headers, limit=MAX_RESPONSE_BYTES
                    )
                    return {
                        "body": body,
                        "final_url": current,
                        "content_type": media,
                        "content_type_header": headers.get("content-type", ""),
                    }
        raise WebError("REDIRECT_LIMIT", "Too many redirects.")
