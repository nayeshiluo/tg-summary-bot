"""Telegram 富文本渲染：保留折叠卡片，降级时转成合法 HTML。

模型的输出是 Markdown + <details>/<summary> 混合语法：
- 优先走 Bot API 的原生 rich message 通道；
- 通道不可用时，转成 Telegram 支持的 <blockquote expandable> HTML。
所有动态文本（群名、昵称、引用内容）必须转义，避免标签注入。
"""
import html
import re
import uuid
from html.parser import HTMLParser
from urllib.parse import urlsplit

_FORMAT_TAGS = {"details", "summary", "b", "strong", "i", "em", "u", "s", "del", "code", "pre", "blockquote"}
_LINK = re.compile(r"(?<!\\)!?\[([^\]\n]*)\]\(([^)\n]*)\)")


def _protect_bare_ampersands(text):
    return re.sub(r"&(?!#[0-9]+;|#x[0-9a-fA-F]+;|[A-Za-z][A-Za-z0-9]+;)", "&amp;", str(text))


_ESCAPES = re.compile(r"([\\`*_\[\](){}#+.!|>~\-])")


def escape_rich_text(value) -> str:
    """动态名称与引用文本一律当纯文本处理。"""
    return _ESCAPES.sub(r"\\\1", html.escape(str(value), quote=True))


def safe_web_url(value):
    url = html.unescape(str(value))
    if any(ord(c) <= 32 or c in "\\<>\"'" for c in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        _ = parsed.port
    except ValueError:
        return None
    return url


class _RichFilter(HTMLParser):
    def __init__(self, allowed_urls):
        super().__init__(convert_charrefs=False)
        self.allowed_urls = allowed_urls
        self.parts = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        emitted = None
        if tag in _FORMAT_TAGS:
            emitted = tag
            suffix = " expandable" if tag == "blockquote" and any(k == "expandable" for k, _ in attrs) else ""
            self.parts.append("<" + tag + suffix + ">")
        elif tag == "a" and self.allowed_urls is None:
            url = safe_web_url(dict(attrs).get("href", ""))
            if url:
                emitted = "a"
                self.parts.append('<a href="' + html.escape(url, quote=True) + '">')
        elif tag == "br":
            self.parts.append("\n")
        if tag != "br":
            self.stack.append((tag, emitted))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is None:
            return
        for _, emitted in reversed(self.stack[index:]):
            if emitted:
                self.parts.append("</" + emitted + ">")
        del self.stack[index:]

    def handle_data(self, data):
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name):
        self.parts.append(escape_rich_text(html.unescape("&" + name + ";")))

    def handle_charref(self, name):
        self.parts.append(escape_rich_text(html.unescape("&#" + name + ";")))

    def finish(self):
        for _, emitted in reversed(self.stack):
            if emitted:
                self.parts.append("</" + emitted + ">")
        return "".join(self.parts)


def sanitize_rich(content, *, allowed_urls=None):
    """保留格式标签，只放行本次记录中真实存在的来源链接。"""
    parser = _RichFilter(allowed_urls)
    parser.feed(_protect_bare_ampersands(content))
    parser.close()
    text = parser.finish()
    replacements = {}
    nonce = uuid.uuid4().hex

    def protect(value):
        key = "\x00" + nonce + str(len(replacements)) + "\x00"
        replacements[key] = value
        return key

    def link(match):
        label, raw_url = match.groups()
        url = safe_web_url(raw_url)
        if url and (allowed_urls is None or url in allowed_urls):
            return protect("[" + label + "](" + url + ")")
        return escape_rich_text(html.unescape(label))

    text = _LINK.sub(link, text)
    # 引用式链接不是允许的传输格式
    text = re.sub(r"(?m)^\s*\[[^\]\n]+\]:[^\n]*$", "", text)
    text = re.sub(r"(?<!\\)\[([^\]\n]+)\]\[[^\]\n]*\]", lambda m: escape_rich_text(html.unescape(m.group(1))), text)
    if allowed_urls is not None:
        # 裸链接也清掉，防止注入的 URL 被自动识别成链接
        text = re.sub(r"(?:https?://|www\.)[^\s<>\]\)]+", "", text, flags=re.IGNORECASE)
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text.strip()


def _inline_html(text):
    """只渲染受支持的 Markdown，其余文本全部转义。"""
    tokens = {}
    nonce = uuid.uuid4().hex

    def protect(value):
        key = "\x00" + nonce + str(len(tokens)) + "\x00"
        tokens[key] = value
        return key

    text = re.sub(r"\\(.)", lambda m: protect(html.escape(m.group(1))), text)

    def link(match):
        label, raw_url = match.groups()
        url = safe_web_url(raw_url)
        plain_label = html.escape(label)
        if url:
            return protect('<a href="' + html.escape(url, quote=True) + '">' + plain_label + "</a>")
        return protect(plain_label)

    text = _LINK.sub(link, text)
    text = re.sub(r"`([^`\n]+)`", lambda m: protect("<code>" + html.escape(m.group(1)) + "</code>"), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", text)
    for key, value in reversed(list(tokens.items())):
        text = text.replace(key, value)
    return text


class _HTMLFallback(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        mapping = {"details": "blockquote", "summary": "b", "strong": "b", "em": "i", "del": "s"}
        emitted = mapping.get(tag, tag) if tag in _FORMAT_TAGS else None
        suffix = " expandable" if tag == "details" or (tag == "blockquote" and any(k == "expandable" for k, _ in attrs)) else ""
        if tag == "a":
            url = safe_web_url(dict(attrs).get("href", ""))
            if url:
                emitted = "a"
                suffix = ' href="' + html.escape(url, quote=True) + '"'
        if tag == "br":
            self.parts.append("\n")
            return
        if emitted:
            self.parts.append("<" + emitted + suffix + ">")
        self.stack.append((tag, emitted))

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is None:
            return
        for original, emitted in reversed(self.stack[index:]):
            if emitted:
                self.parts.append("</" + emitted + ">")
            if original == "summary":
                self.parts.append("\n")
        del self.stack[index:]

    def handle_data(self, data):
        if any(emitted in {"code", "pre"} for _, emitted in self.stack):
            self.parts.append(html.escape(data, quote=False))
        else:
            self.parts.append(_inline_html(data))

    def finish(self):
        for _, emitted in reversed(self.stack):
            if emitted:
                self.parts.append("</" + emitted + ">")
        return "".join(self.parts)


def rich_to_html(content: str) -> str:
    parser = _HTMLFallback()
    parser.feed(_protect_bare_ampersands(content))
    parser.close()
    return parser.finish()
