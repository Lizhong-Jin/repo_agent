"""Conservative, bounded HTML-to-Markdown and text snapshot extraction."""

import asyncio
import json
import re
from dataclasses import dataclass, field
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import urljoin

from .web_errors import WebError
from .web_http import public_url

MAX_TEXT_CHARS = 5 * 1024 * 1024
MAX_LINKS = 128
LINE_WIDTH = 512
MAX_LINES = 100_000
_VOID = {
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
_SKIP = {"head", "script", "style", "template", "noscript", "svg", "canvas", "nav", "footer"}
_BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "header",
    "aside",
    "dl",
    "dt",
    "dd",
    "figure",
    "figcaption",
    "details",
    "summary",
    "ul",
    "ol",
}


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 100_000 or len(self.stack) > 128:
            raise WebError("CONTENT_TOO_COMPLEX", "HTML structure exceeds extraction limits.")
        node = _Node(
            tag, {key: value for key, value in attrs if key in {"href", "class", "alt", "role"}}
        )
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.nodes += 1
        if self.nodes > 100_000:
            raise WebError("CONTENT_TOO_COMPLEX", "HTML structure exceeds extraction limits.")
        self.stack[-1].children.append(data)


def _text(node):
    if isinstance(node, str):
        return node
    return "".join(_text(child) for child in node.children)


def _walk(node):
    if not isinstance(node, str):
        yield node
        for child in node.children:
            yield from _walk(child)


def _fence(text):
    return "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", text)), default=0))


def _table_rows(node):
    for child in node.children:
        if isinstance(child, _Node):
            if child.tag == "tr":
                yield child
            elif child.tag in {"thead", "tbody", "tfoot"}:
                yield from _table_rows(child)


def _content_roots(node, *, articles=False):
    if isinstance(node, str):
        return
    if (
        node.tag == "article"
        if articles
        else (node.tag == "main" or node.attrs.get("role") == "main")
    ):
        yield node
        return
    if node.tag not in _SKIP:
        for child in node.children:
            yield from _content_roots(child, articles=articles)


def _tidy_markdown(text):
    lines = []
    fence = None
    for line in text.split("\n"):
        if fence is not None:
            lines.append(line)
            if line.strip() == fence:
                fence = None
            continue
        match = re.fullmatch(r"(`{3,})[a-zA-Z0-9_+-]*", line.strip())
        if match:
            fence = match[1]
        if line.strip():
            lines.append(line.rstrip())
        elif lines and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).strip("\n")


class _Markdown:
    def __init__(self, url):
        self.url = url
        self.links = []
        self.seen = set()
        self.links_truncated = False
        self.rendered = 0

    def render(self, node):
        result = self._render(node)
        # Bound aggregate work, including intermediate strings from nested structures.
        self.rendered += len(result)
        if self.rendered > MAX_TEXT_CHARS * 8:
            raise WebError("CONTENT_TOO_COMPLEX", "HTML text expansion exceeds extraction limits.")
        return result

    def _render(self, node):
        if isinstance(node, str):
            return re.sub(r"\s+", " ", node)
        tag = node.tag
        if tag in _SKIP or tag == "title":
            return ""
        if tag == "pre":
            text = _text(node).strip("\n")
            fence = _fence(text)
            code = next(
                (n for n in node.children if isinstance(n, _Node) and n.tag == "code"), node
            )
            match = re.search(r"(?:^|\s)language-([a-zA-Z0-9_+-]+)", code.attrs.get("class") or "")
            language = match[1] if match else ""
            return f"\n\n{fence}{language}\n{text}\n{fence}\n\n"
        if tag == "table":
            rows = []
            for row in _table_rows(node):
                cells = [
                    cell
                    for cell in row.children
                    if isinstance(cell, _Node) and cell.tag in {"td", "th"}
                ]
                if cells:
                    if len(cells) > 64:
                        raise WebError("CONTENT_TOO_COMPLEX", "Table has too many columns.")
                    rows.append(
                        (
                            [
                                self.render(cell).strip().replace("|", "\\|").replace("\n", "<br>")
                                for cell in cells
                            ],
                            any(cell.tag == "th" for cell in cells),
                        )
                    )
            if not rows:
                return ""
            width = max(len(cells) for cells, _ in rows)
            values = [cells + [""] * (width - len(cells)) for cells, _ in rows]
            if not rows[0][1]:
                values.insert(0, [""] * width)
            values.insert(1, ["---"] * width)
            return "\n\n" + "\n".join("| " + " | ".join(cells) + " |" for cells in values) + "\n\n"
        text = "".join(self.render(child) for child in node.children)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return "\n\n" + "#" * int(tag[1]) + " " + text.strip() + "\n\n"
        if tag == "a" and node.attrs.get("href"):
            try:
                link = public_url(urljoin(self.url, node.attrs["href"]))
            except (ValueError, WebError):
                return text
            # Keep fragment anchors in returned links, while requests never send fragments.
            fragment = node.attrs["href"].partition("#")[2]
            if (
                fragment
                and len(link) + len(fragment) + 1 <= 2048
                and re.fullmatch(r"[\w.%:-]+", fragment)
            ):
                link += "#" + fragment
            label = text.strip() or link
            if link not in self.seen:
                if len(self.links) < MAX_LINKS:
                    self.seen.add(link)
                    self.links.append({"text": label[:160], "url": link})
                else:
                    self.links_truncated = True
            return f"[{label}](<{link}>)"
        if tag == "code":
            marker = "`" * max(1, max((len(run) + 1 for run in re.findall(r"`+", text)), default=0))
            return marker + " " + text.strip() + " " + marker
        if tag in {"strong", "b"}:
            return "**" + text + "**"
        if tag in {"em", "i"}:
            return "*" + text + "*"
        if tag == "li":
            return "\n- " + text.strip() + "\n"
        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n\n---\n\n"
        if tag == "img":
            return node.attrs.get("alt") or ""
        if tag in _BLOCK:
            return "\n\n" + text.strip() + "\n\n"
        return text


def _decode(body, header, html):
    message = Message()
    message["content-type"] = header
    charset = message.get_content_charset()
    if body.startswith(b"\xef\xbb\xbf"):
        charset = "utf-8-sig"
    elif body.startswith((b"\xff\xfe", b"\xfe\xff")):
        charset = "utf-16"
    elif not charset and html:
        match = re.search(rb"<meta\s[^>]*charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", body[:4096], re.I)
        charset = match[1].decode("ascii") if match else None
    charset = (charset or "utf-8").lower()
    allowed = {
        "utf-8",
        "utf8",
        "utf-8-sig",
        "utf-16",
        "utf-16le",
        "utf-16be",
        "ascii",
        "iso-8859-1",
        "latin1",
        "windows-1252",
        "cp1252",
        "gb2312",
        "gbk",
        "gb18030",
        "big5",
        "shift_jis",
        "euc-jp",
    }
    if charset not in allowed:
        raise WebError("UNSUPPORTED_ENCODING", "Page character encoding is not supported.")
    try:
        text = body.decode(charset)
    except UnicodeError:
        raise WebError("INVALID_RESPONSE", "Page has invalid text encoding.") from None
    if "\x00" in text:
        raise WebError("UNSUPPORTED_CONTENT_TYPE", "Binary content is not supported.")
    return text


async def extract_page(body: bytes, content_type: str, content_type_header: str, final_url: str):
    text = _decode(body, content_type_header, content_type == "text/html")
    title = ""
    links = []
    links_truncated = False
    if content_type == "text/html":
        parser = _Document()
        for offset in range(0, len(text), 16_384):
            parser.feed(text[offset : offset + 16_384])
            if len(parser.rawdata) > 65_536:
                raise WebError("CONTENT_TOO_COMPLEX", "HTML token exceeds extraction limits.")
            await asyncio.sleep(0)
        parser.close()
        title = next((_text(node) for node in _walk(parser.root) if node.tag == "title"), "")
        renderer = _Markdown(final_url)
        candidates = list(_content_roots(parser.root)) or list(
            _content_roots(parser.root, articles=True)
        )
        # Prefer explicit document landmarks; retain the full document for short/ambiguous bodies.
        root = (
            _Node("document", children=candidates)
            if sum(len(_text(node).strip()) for node in candidates) >= 200
            else parser.root
        )
        text = _tidy_markdown(renderer.render(root))
        links, links_truncated = renderer.links, renderer.links_truncated
    elif content_type == "application/json" or content_type.endswith("+json"):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2, allow_nan=False)
        except (ValueError, RecursionError):
            raise WebError(
                "INVALID_RESPONSE", "Page contains invalid or overly nested JSON."
            ) from None
    if len(text) > MAX_TEXT_CHARS:
        raise WebError("RESPONSE_TOO_LARGE", "Extracted text exceeds the size limit.")
    # Stable normalized lines, including very long minified text/JSON/code lines.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c.isprintable() or c in "\n\t")
    lines = []
    wrapped = False
    if text.count("\n") > MAX_LINES:
        raise WebError("RESPONSE_TOO_LARGE", "Page exceeds the cached line limit.")
    for line in text.strip("\n").splitlines() if text else []:
        if len(line) > LINE_WIDTH:
            wrapped = True
        lines.extend(line[i : i + LINE_WIDTH] for i in range(0, max(1, len(line)), LINE_WIDTH))
        if len(lines) > MAX_LINES:
            raise WebError("RESPONSE_TOO_LARGE", "Page exceeds the cached line limit.")
    return {
        "title": " ".join(title.split())[:300],
        "lines": tuple(lines),
        "links": links,
        "links_truncated": links_truncated,
        "long_lines_wrapped": wrapped,
    }
