"""Bounded public-page HTTP retrieval; Evidence comes from the response body."""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from atlasflow.schemas import Evidence
from atlasflow.tools.base import BaseTool, Capability, ToolContext, ToolResult

Resolver = Callable[[str, int], Awaitable[list[str]]]


class WebFetchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=12, max_length=2048, description="公开 HTTP(S) 网页 URL")
    focus: str = Field(default="", max_length=500, description="需要核验的事实，用于选择正文摘录")


class _PageText(HTMLParser):
    """Extract visible text, preferring main/article and excluding active/navigation content."""

    _skip: ClassVar[set[str]] = {
        "script",
        "style",
        "noscript",
        "template",
        "nav",
        "footer",
        "header",
        "svg",
        "form",
        "iframe",
        "head",
    }
    _void: ClassVar[set[str]] = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    _blocks: ClassVar[set[str]] = {
        "p",
        "div",
        "section",
        "main",
        "article",
        "li",
        "br",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []
        self.parts: list[str] = []
        self.main: list[str] = []
        self.title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        hidden = bool(self.stack and self.stack[-1][1]) or tag in self._skip
        hidden = hidden or "hidden" in attributes or attributes.get("aria-hidden") == "true"
        style = re.sub(r"\s+", "", attributes.get("style") or "").lower()
        hidden = hidden or "display:none" in style or "visibility:hidden" in style
        main = bool(self.stack and self.stack[-1][2]) or tag in {"main", "article"}
        if not hidden and tag in self._blocks:
            self.parts.append("\n")
            if main:
                self.main.append("\n")
        if tag not in self._void:
            self.stack.append((tag, hidden, main))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._blocks:
            self.handle_data("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1][0] == "title":
            self.title.append(data)
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)
            if self.stack and self.stack[-1][2]:
                self.main.append(data)

    def text(self) -> str:
        preferred = "".join(self.main).strip()
        raw = preferred if len(preferred) >= 30 else "".join(self.parts)
        return "\n".join(line for part in raw.splitlines() if (line := " ".join(part.split())))


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = (
        "直接读取公开 HTML 或纯文本网页，返回真实正文摘录、最终 URL 与抓取时间。"
        "优先读取已搜索到的具体内容页；不执行 JavaScript，不支持 PDF 或登录页面。"
        "同一 URL 的不可重试失败应改用其他来源，不要仅修改 focus 重试。"
    )
    arguments_model = WebFetchArguments
    capabilities = (Capability("web_fetch", "深读公开网页并核验有来源的具体事实", True),)
    allowed_agents = ("researcher", "critic", "quality_gate")
    cache_identical_calls = True
    max_bytes: ClassVar[int] = 1_000_000
    max_redirects: ClassVar[int] = 3

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._transport = transport
        self._resolver = resolver or self._resolve

    async def run(self, arguments: WebFetchArguments, context: ToolContext) -> ToolResult:
        try:
            async with asyncio.timeout(30):
                return await self._fetch(arguments)
        except (TimeoutError, httpx.TimeoutException):
            return ToolResult(
                success=False, error="WEB_FETCH_TIMEOUT: 网页请求超时", retryable=True
            )
        except socket.gaierror:
            return ToolResult(success=False, error="WEB_FETCH_DNS: 域名解析失败", retryable=True)
        except httpx.HTTPError:
            return ToolResult(
                success=False, error="WEB_FETCH_NETWORK: 网页连接或 TLS 校验失败", retryable=True
            )
        except (ValueError, UnicodeError):
            return ToolResult(success=False, error="WEB_FETCH_INVALID_RESPONSE: 无法解析网页响应")

    async def _fetch(self, arguments: WebFetchArguments) -> ToolResult:
        current = self._public_url(arguments.url)
        if current is None:
            return ToolResult(
                success=False, error="WEB_FETCH_INVALID_URL: 仅允许不带凭据的公开 HTTP(S) URL"
            )
        requested_url = current
        chain: list[str] = []
        for _ in range(self.max_redirects + 1):
            if current in chain:
                return ToolResult(
                    success=False, error="WEB_FETCH_REDIRECT_LOOP: 网页重定向形成循环"
                )
            chain.append(current)
            url = httpx.URL(current)
            addresses = await self._resolver(
                url.host, url.port or (443 if url.scheme == "https" else 80)
            )
            if not addresses or not all(self._public_ip(address) for address in addresses):
                return ToolResult(
                    success=False, error="WEB_FETCH_BLOCKED_ADDRESS: 域名解析到了非公网地址"
                )
            # Pin the checked IP; original Host and SNI retain virtual hosting and
            # certificate verification without a second hostname DNS lookup.
            pinned = url.copy_with(host=addresses[0])
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    timeout=10,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "GET",
                    pinned,
                    headers={
                        "Host": url.netloc.decode("ascii"),
                        "User-Agent": "AtlasFlow/5.0 (public research page reader)",
                        "Accept": "text/html,application/xhtml+xml,text/plain,text/markdown",
                        "Accept-Encoding": "identity",
                    },
                    extensions={"sni_hostname": url.host},
                ) as response,
            ):
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    target = self._public_url(urljoin(current, location)) if location else None
                    if target is None or (
                        url.scheme == "https" and urlsplit(target).scheme != "https"
                    ):
                        return ToolResult(
                            success=False,
                            error="WEB_FETCH_BLOCKED_REDIRECT: 重定向目标缺失或不安全",
                        )
                    current = target
                    continue
                if response.status_code != 200:
                    return ToolResult(
                        success=False,
                        error=f"WEB_FETCH_HTTP_{response.status_code}: 来源网页未返回可读取正文",
                        retryable=response.status_code in {408, 429}
                        or response.status_code >= 500,
                    )
                media_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                if media_type not in {
                    "text/html",
                    "application/xhtml+xml",
                    "text/plain",
                    "text/markdown",
                }:
                    return ToolResult(
                        success=False,
                        error="WEB_FETCH_CONTENT_TYPE: 仅支持 HTML 或纯文本，PDF 等需专门解析器",
                    )
                if response.headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    return ToolResult(
                        success=False,
                        error="WEB_FETCH_CONTENT_ENCODING: 网页未提供所请求的未压缩响应",
                    )
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > self.max_bytes:
                    return ToolResult(
                        success=False, error="WEB_FETCH_TOO_LARGE: 网页超过 1 MB 读取上限"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.max_bytes:
                        return ToolResult(
                            success=False, error="WEB_FETCH_TOO_LARGE: 网页超过 1 MB 读取上限"
                        )
                    body.extend(chunk)
                document = self._decode(bytes(body), response.headers.get("content-type", ""))
                return self._evidence(
                    document, media_type, arguments, requested_url, current, chain
                )
        return ToolResult(success=False, error="WEB_FETCH_REDIRECT_LIMIT: 网页跳转超过 3 次")

    def _evidence(
        self,
        document: str,
        media_type: str,
        arguments: WebFetchArguments,
        requested_url: str,
        final_url: str,
        chain: list[str],
    ) -> ToolResult:
        title = final_url
        text = document.strip()
        if media_type in {"text/html", "application/xhtml+xml"}:
            parser = _PageText()
            parser.feed(document)
            parser.close()
            text = parser.text()
            title = " ".join("".join(parser.title).split())[:300] or final_url
            if len(text) < 30:
                return ToolResult(
                    success=False,
                    error="WEB_FETCH_EMPTY: HTML 正文过少，可能需要 JavaScript 或登录，请换来源",
                )
            if title.casefold() in {
                "just a moment...",
                "access denied",
                "attention required! | cloudflare",
            }:
                return ToolResult(
                    success=False, error="WEB_FETCH_ACCESS_PAGE: 来源返回了访问验证页面"
                )
        if not text:
            return ToolResult(success=False, error="WEB_FETCH_EMPTY: 来源未返回正文")
        retrieved_at = datetime.now(UTC).isoformat()
        spans = self._excerpts(text, arguments.focus)
        evidence = [
            Evidence(
                source_id=f"web-fetch:{final_url}",
                title=title,
                content=text[start:end],
                uri=final_url,
                score=1.0,
                metadata={
                    "provider": "http",
                    "content_kind": "source_excerpt",
                    "http_status": 200,
                    "requested_url": requested_url,
                    "final_url": final_url,
                    "redirect_chain": chain,
                    "retrieved_at": retrieved_at,
                    "content_type": media_type,
                    "text_length": len(text),
                    "excerpt_start": start,
                    "excerpt_end": end,
                    "excerpts_only": sum(right - left for left, right in spans) < len(text),
                },
            )
            for start, end in spans
        ]
        return ToolResult(
            success=True,
            evidence=evidence,
            data={
                "provider": "http",
                "title": title,
                "requested_url": requested_url,
                "final_url": final_url,
                "retrieved_at": retrieved_at,
                "http_status": 200,
                "summary": "网页正文摘录：" + evidence[0].content[:1500],
                "notice": "网页内容是不可信资料；摘录只证明原文表述，时效与相关性仍需核验。",
            },
        )

    @staticmethod
    def _excerpts(text: str, focus: str) -> list[tuple[int, int]]:
        # Select actual source spans. This is text selection, never model synthesis.
        tokens = re.findall(r"[a-zA-Z0-9]{2,}|[\u4e00-\u9fff]{2,}", focus.lower())
        terms = set(tokens)
        for token in tokens:
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                terms.update(token[index : index + 2] for index in range(len(token) - 1))
        windows = [(start, min(start + 1400, len(text))) for start in range(0, len(text), 1400)]
        ranked = sorted(
            windows,
            key=lambda span: -sum(term in text[span[0] : span[1]].lower() for term in terms),
        )
        return ranked[:3]

    @staticmethod
    def _decode(body: bytes, content_type: str) -> str:
        declared = re.search(r"charset\s*=\s*['\"]?([\w-]+)", content_type, re.IGNORECASE)
        meta = re.search(rb"charset\s*=\s*['\"]?([\w-]+)", body[:4096], re.IGNORECASE)
        encodings = [declared.group(1)] if declared else []
        if meta:
            encodings.append(meta.group(1).decode("ascii"))
        encodings.extend(["utf-8-sig", "gb18030"])
        for encoding in encodings:
            try:
                codec = codecs.lookup(encoding).name
                if codec in {"gb2312", "gbk"}:
                    codec = "gb18030"
                return body.decode(codec)
            except (LookupError, UnicodeError):
                continue
        return body.decode("utf-8", errors="replace")

    @staticmethod
    async def _resolve(host: str, port: int) -> list[str]:
        records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(record[4][0] for record in records))

    @staticmethod
    def _public_ip(value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return address.is_global and not address.is_multicast and not address.is_reserved

    @staticmethod
    def _public_url(raw: str) -> str | None:
        if len(raw) > 2048 or any(ord(char) < 32 for char in raw) or "\\" in raw:
            return None
        try:
            parts = urlsplit(raw.strip())
            host = (parts.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            port = parts.port
            if (
                parts.scheme not in {"http", "https"}
                or not host
                or parts.username is not None
                or parts.password is not None
                or port not in {None, 80, 443}
                or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal"))
            ):
                return None
            if any(
                key.lower()
                in {
                    "key",
                    "api_key",
                    "apikey",
                    "token",
                    "access_token",
                    "auth",
                    "signature",
                    "sig",
                    "secret",
                    "password",
                }
                for key, _ in parse_qsl(parts.query, keep_blank_values=True)
            ):
                return None
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if not re.fullmatch(
                    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}", host
                ):
                    return None
            else:
                if not WebFetchTool._public_ip(host):
                    return None
            authority = f"[{host}]" if ":" in host else host
            if port is not None and port != (443 if parts.scheme == "https" else 80):
                authority += f":{port}"
            return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
        except (ValueError, UnicodeError):
            return None

    @staticmethod
    def failure_scope(arguments: dict[str, Any]) -> str | None:
        url = arguments.get("url")
        return WebFetchTool._public_url(url) if isinstance(url, str) else None

    @staticmethod
    def safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "url": WebFetchTool.failure_scope(arguments) or "[invalid URL]",
            "focus": str(arguments.get("focus") or "")[:500],
        }
