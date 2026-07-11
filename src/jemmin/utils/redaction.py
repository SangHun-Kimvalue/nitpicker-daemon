"""Secret redaction — 클라우드 전송 payload에서 민감 정보를 마스킹합니다.

주의: 전체 워킹트리 스캔이 아니라, 클라우드로 전송되는 payload만 대상으로 합니다.
V29 설계 원칙 §12.6 준수.
"""
from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# 패턴 정의: (이름, 컴파일된 regex, 대체 문자열)
#
# 그룹 구조:
#   - 전체 치환 패턴(aws_access_key, private_key 등): 그룹 없이 전체 매치를 치환
#   - 키=값 패턴(password, token 등): \1 = prefix(키 이름), \2 = 값(마스킹 대상)
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: Final[list[tuple[str, re.Pattern[str], str]]] = [
    # AWS Access Key ID (AKIA...)
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED]",
    ),
    # API Key (api_key = ..., apikey: ...)
    (
        "api_key",
        re.compile(
            r"(?i)(\bapi[_-]?key\s*[:=]\s*)['\"]?([A-Za-z0-9\-_./+]{8,})['\"]?",
            re.MULTILINE,
        ),
        r"\1[REDACTED]",
    ),
    # Password (password = ..., passwd: ...)
    (
        "password",
        re.compile(
            r"(?i)(\bpass(?:word|wd)?\s*[:=]\s*)['\"]?([^\s'\"]{4,})['\"]?",
            re.MULTILINE,
        ),
        r"\1[REDACTED]",
    ),
    # Secret / Secret Key (secret = ..., secret_key: ...)
    (
        "secret",
        re.compile(
            r"(?i)(\bsecret(?:[_-]?key)?\s*[:=]\s*)['\"]?([A-Za-z0-9\-_./+]{8,})['\"]?",
            re.MULTILINE,
        ),
        r"\1[REDACTED]",
    ),
    # Token (token = ..., access_token: ...)
    (
        "token",
        re.compile(
            r"(?i)(\b(?:access_)?token\s*[:=]\s*)['\"]?([A-Za-z0-9\-_./+]{8,})['\"]?",
            re.MULTILINE,
        ),
        r"\1[REDACTED]",
    ),
    # Authorization / Auth header (auth = ..., authorization: Bearer ...)
    (
        "auth",
        re.compile(
            r"(?i)(\bauth(?:orization)?\s*[:=]\s*)['\"]?([^\s'\"]{8,})['\"]?",
            re.MULTILINE,
        ),
        r"\1[REDACTED]",
    ),
    # Bearer token in HTTP headers
    (
        "bearer",
        re.compile(
            r"(?i)(Bearer\s+)([A-Za-z0-9\-_./+]{8,})",
            re.MULTILINE,
        ),
        r"\1[REDACTED]",
    ),
    # PEM 형식 Private Key 블록 (RSA, EC, DSA, OPENSSH 등)
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
            re.MULTILINE,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # DB 연결 문자열의 비밀번호 부분 (postgresql://user:password@host)
    (
        "db_url",
        re.compile(
            r"(?i)((?:postgresql|mysql|mongodb|redis)://[^:]+:)([^@\s]{1,})(@)",
            re.MULTILINE,
        ),
        r"\1[REDACTED]\3",
    ),
]


def redact_secrets(text: str) -> str:
    """text 내 민감 정보 패턴을 [REDACTED]로 치환합니다.

    클라우드로 전송되는 payload에만 적용하며,
    전체 워킹트리 스캔에는 사용하지 않습니다.

    Returns:
        민감 정보가 마스킹된 문자열.
    """
    for _name, pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
