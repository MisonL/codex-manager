"""OpenAI Sentinel proof-of-work 令牌解算器"""

import base64
import hashlib
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence, Optional

DEFAULT_SENTINEL_DIFF = "0fffff"
DEFAULT_MAX_ITERATIONS = 500_000
_SCREEN_SIGNATURES = (3000, 3120, 4000, 4160)
_LANGUAGE_SIGNATURE = "en-US,es-US,en,es"
_NAVIGATOR_KEYS = ("location", "ontransitionend", "onprogress")
_WINDOW_KEYS = ("window", "document", "navigator")

class SentinelPOWError(RuntimeError):
    """当无法解算 PoW 令牌时抛出"""

def _format_browser_time() -> str:
    """模拟浏览器风格的时间戳 (EST 时区)"""
    browser_now = datetime.now(timezone(timedelta(hours=-5)))
    return browser_now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"

def build_sentinel_config(user_agent: str) -> list:
    """构建用于 PoW 解算的浏览器指纹载荷"""
    perf_ms = time.perf_counter() * 1000
    epoch_ms = (time.time() * 1000) - perf_ms
    return [
        random.choice(_SCREEN_SIGNATURES),
        _format_browser_time(),
        4294705152,
        0,
        user_agent,
        "",
        "",
        "en-US",
        _LANGUAGE_SIGNATURE,
        0,
        random.choice(_NAVIGATOR_KEYS),
        "location",
        random.choice(_WINDOW_KEYS),
        perf_ms,
        str(uuid.uuid4()),
        "",
        8,
        epoch_ms,
    ]

def _encode_pow_payload(config: Sequence[object], nonce: int) -> bytes:
    """按特定顺序编码载荷"""
    prefix = (json.dumps(config[:3], separators=(",", ":"), ensure_ascii=False)[:-1] + ",").encode("utf-8")
    middle = (
        "," + json.dumps(config[4:9], separators=(",", ":"), ensure_ascii=False)[1:-1] + ","
    ).encode("utf-8")
    suffix = ("," + json.dumps(config[10:], separators=(",", ":"), ensure_ascii=False)[1:]).encode("utf-8")
    body = prefix + str(nonce).encode("ascii") + middle + str(nonce >> 1).encode("ascii") + suffix
    return base64.b64encode(body)

def solve_sentinel_pow(
    seed: str,
    difficulty: str,
    config: Sequence[object],
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> str:
    """核心算法：解算 Sentinel PoW 挑战"""
    seed_bytes = seed.encode("utf-8")
    target = bytes.fromhex(difficulty)
    prefix_length = len(target)

    for nonce in range(max_iterations):
        encoded = _encode_pow_payload(config, nonce)
        # OpenAI 目前使用 SHA3-512 算法
        digest = hashlib.sha3_512(seed_bytes + encoded).digest()
        if digest[:prefix_length] <= target:
            return encoded.decode("ascii")

    raise SentinelPOWError(f"failed to solve sentinel pow after {max_iterations} attempts")

def get_sentinel_p_token(
    user_agent: str,
    difficulty: str = DEFAULT_SENTINEL_DIFF,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> str:
    """生成 Sentinel 请求所需的 'p' 令牌"""
    config = build_sentinel_config(user_agent)
    seed = format(random.random())
    try:
        solution = solve_sentinel_pow(seed, difficulty, config, max_iterations=max_iterations)
        return f"gAAAAAC{solution}"
    except Exception:
        return ""
