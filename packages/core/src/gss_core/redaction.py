"""统一脱敏层（宪法第 I 条）。

**所有**日志、异常、审计与对外响应都必须经过 :func:`redact` / :func:`redact_mapping`，
以避免 Access Token、Cookie、卡字段、代理口令进入日志或数据库。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, List, Mapping, Pattern, Tuple

MASK = "***"

_TOKEN_FIELD = (
    r"(?:access|refresh|id)[_-]?token|session(?:[_-]?token)?|"
    r"(?:__Secure-|__Host-)?next-auth\.session-token(?:\.\d+)?"
)
_HEADER_FIELD = r"cookies?|set-cookie|authorization"
_CREDENTIAL_KEY = re.compile(rf"(?:{_TOKEN_FIELD}|{_HEADER_FIELD})", re.I)

# 同时支持 JSON 双引号和日志中 Python repr 的单引号。上游有时截短错误体，
# 即使结束引号丢失，也要屏蔽到本行行尾。
_QUOTED = r'"(?:\\[^\r\n]|[^"\\\r\n])*"?' + "|" + r"'(?:\\[^\r\n]|[^'\\\r\n])*'?"


def _credential_pair(names: str, unquoted: str) -> Pattern[str]:
    return re.compile(
        rf"""(?P<prefix>(?<!\w)(?:{names})(?:["']\s*[:=]\s*|[ \t]*[:=][ \t]*))"""
        rf"(?P<bytes_marker>b(?=[\"']))?(?P<value>{_QUOTED}|{unquoted})",
        re.I,
    )


# 裸 HTTP 头必须整行屏蔽，不能在第一个分号截断，留下后续 Cookie。
_HEADER_PAIR = _credential_pair(_HEADER_FIELD, r"[^\r\n]+")
_TOKEN_PAIR = _credential_pair(_TOKEN_FIELD, r"""[\[{][^\r\n]*|[^\s"'{}[\],;]+""")


def _mask_pair(match: re.Match[str]) -> str:
    value = match.group("value")
    quote = value[0] if value[0] in "\"'" else ""
    return match.group("prefix") + (match.group("bytes_marker") or "") + quote + MASK + quote


#: 带上下文的键值对脱敏规则：(正则, 替换模板)。
_KEYED_RULES: List[Tuple[Pattern[str], str]] = [
    (
        re.compile(r"(?i)\b(client[_-]?secret)\b(\"?\s*[:=]\s*\"?)([^\s,;&\"']{6,})"),
        r"\1\2" + MASK,
    ),
    (
        re.compile(r"\b(pi_[A-Za-z0-9]+)_secret_[A-Za-z0-9]+"),
        r"\1_secret_" + MASK,
    ),
    (
        re.compile(r"(?i)\b(api[_-]?key|service[_-]?token|secret)\b(\"?\s*[:=]\s*\"?)([A-Za-z0-9._\-]{6,})"),
        r"\1\2" + MASK,
    ),
    (
        re.compile(r"(?i)\b(email)\b(\"?\s*[:=]\s*\"?)([^\s,;&\"']{3,})"),
        r"\1\2" + MASK,
    ),
    (
        re.compile(r"(?i)\b(line1|line2|postal_code|address_line1)\b(\"?\s*[:=]\s*\"?)([^\s,;&\"']{2,})"),
        r"\1\2" + MASK,
    ),
    (
        re.compile(r"(?i)\b(password|passwd|pwd)\b(\"?\s*[:=]\s*\"?)([^\s,;\"']{3,})"),
        r"\1\2" + MASK,
    ),
    (
        re.compile(r"(?i)\b(cvv|cvc|cvv2|card[_-]?number|pan|cardno)\b(\"?\s*[:=]\s*\"?)([0-9 ]{3,19})"),
        r"\1\2" + MASK,
    ),
]

#: 无上下文的裸 Bearer / Basic 头。
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{6,}")

#: 代理 URL 中的口令：scheme://user:pass@host
_PROXY_CRED = re.compile(r"(?i)(https?://[^:/\s@]+:)[^@/\s]+(@)")

#: 疑似完整信用卡号（13–19 位数字，允许空格/短横线分隔），要求有卡相关上下文。
_PAN_CONTEXT = re.compile(
    r"(?i)\b(card[_-]?(number|no)|pan)\b[^\d]{0,12}((?:\d[ -]?){13,19})"
)

#: JWT（三段式）——常见于 session token。
_JWT = re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}\b")


def redact(value: str) -> str:
    """对单个字符串脱敏。"""

    if not value:
        return value
    text = _HEADER_PAIR.sub(_mask_pair, str(value))
    text = _TOKEN_PAIR.sub(_mask_pair, text)
    for pattern, repl in _KEYED_RULES:
        text = pattern.sub(repl, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _PROXY_CRED.sub(r"\1" + MASK + r"\2", text)
    text = _PAN_CONTEXT.sub(lambda m: f"{m.group(1)}{MASK}", text)
    text = _JWT.sub(MASK, text)
    return text


def redact_mapping(obj: Any) -> Any:
    """递归脱敏字典 / 列表 / 标量。凭证键的值整体替换，不保留其中的嵌套字段。"""

    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, Mapping):
        return {
            k: MASK if isinstance(k, str) and _CREDENTIAL_KEY.fullmatch(k) else redact_mapping(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple, set)):
        return type(obj)(redact_mapping(v) for v in obj)
    return obj


class RedactingFilter(logging.Filter):
    """logging 过滤器：把已格式化消息中的敏感值替换掉。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 防御性
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def install_logging_filter(logger: logging.Logger | None = None) -> None:
    """给指定 logger（默认 root）挂上脱敏过滤器，幂等。"""

    target = logger or logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(RedactingFilter())
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


SENSITIVE_KEYS: Iterable[str] = (
    "access_token",
    "refresh_token",
    "session",
    "cookie",
    "authorization",
    "api_key",
    "password",
    "cvv",
    "card_number",
)


# 别名：让代码更易读
redact_sensitive = redact


__all__ = [
    "MASK",
    "redact",
    "redact_sensitive",
    "redact_mapping",
    "RedactingFilter",
    "install_logging_filter",
    "SENSITIVE_KEYS",
]
