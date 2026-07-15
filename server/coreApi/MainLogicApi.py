import json
import logging
import os
import re
import time
import uuid
import random
import threading
import datetime
import ipaddress
import socket
from typing import Dict, Any, List, Optional
from urllib.parse import parse_qs, quote, urlparse, urlunparse

import requests

from server.util.Config import ConfigManager
from server.util.CryptoUtils import create_sign, aes_encrypt, aes_decrypt
from server.util.CaptchaUtils import recognize_blockPuzzle_captcha, recognize_clickWord_captcha
from server.util.HelperFunctions import get_current_month_info
from server.proxy_settings import load_global_proxy_settings

logger = logging.getLogger(__name__)

DEFAULT_MOGUDING_PROXY_API_TIMEOUT_SECONDS = 10.0
DEFAULT_MOGUDING_PROXY_TTL_SECONDS = 55.0
MAX_MOGUDING_PROXY_API_TIMEOUT_SECONDS = 30.0
MAX_MOGUDING_PROXY_TTL_SECONDS = 600.0
DEFAULT_MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS = 600.0
MAX_MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS = 3600.0
_IP_RESTRICTION_LOCK = threading.Lock()
_IP_RESTRICTION_UNTIL = 0.0
_IP_RESTRICTION_REASON = ""


def _split_proxy_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items: List[str] = []
        for item in value:
            items.extend(_split_proxy_values(item))
        return items
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[\s,;]+", text) if item.strip()]


def _normalize_proxy_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return text


def _parse_float_env(name: str, default: float, max_value: float) -> float:
    try:
        value = float(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return default
    if value < 0:
        return 0.0
    return min(value, max_value)


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _split_env_list(value: str) -> List[str]:
    return [item.strip().lower() for item in (value or "").replace(";", ",").split(",") if item.strip()]


def _host_matches_allowlist(host: str, allowed_hosts: List[str]) -> bool:
    normalized = (host or "").strip().lower()
    if not allowed_hosts:
        return True
    if "*" in allowed_hosts or normalized in allowed_hosts:
        return True
    return any(item.startswith(".") and normalized.endswith(item) for item in allowed_hosts)


def _is_private_or_special_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except Exception:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _proxy_fetch_url_is_private_or_special(host: str, port: int) -> bool:
    normalized = (host or "").strip().lower()
    if not normalized or normalized == "localhost":
        return True
    try:
        ipaddress.ip_address(normalized)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    except Exception:
        return True

    if is_ip_literal:
        return _is_private_or_special_ip(normalized)
    try:
        infos = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
    except Exception:
        return True
    return any(_is_private_or_special_ip(info[4][0]) for info in infos)


def _is_safe_proxy_fetch_url(fetch_url: str) -> bool:
    parsed = urlparse(str(fetch_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    allowed_hosts = _split_env_list(os.getenv("MOGUDING_PROXY_ALLOWED_HOSTS") or "")
    if not _host_matches_allowlist(host, allowed_hosts):
        return False
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    private_endpoint = _proxy_fetch_url_is_private_or_special(host, port)
    if private_endpoint:
        return _env_flag("ALLOW_PRIVATE_MOGUDING_PROXY_ENDPOINTS") and bool(allowed_hosts)
    return True


def _extract_proxy_host_port(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for line in text.splitlines():
        item = line.strip()
        if not item:
            continue
        match = re.search(r"(?P<host>(?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9.-]+):(?P<port>\d{1,5})", item)
        if not match:
            continue
        try:
            port = int(match.group("port"))
        except Exception:
            continue
        if 1 <= port <= 65535:
            return f"{match.group('host')}:{port}"
    return ""


def _proxy_auth_from_fetch_url(fetch_url: str) -> tuple[str, str]:
    try:
        params = parse_qs(urlparse(fetch_url).query, keep_blank_values=True)
        username = str((params.get("accessName") or [""])[0] or "").strip()
        password = str((params.get("accessPassword") or [""])[0] or "").strip()
        return username, password
    except Exception:
        return "", ""


def _apply_proxy_auth_from_fetch_url(proxy_url: str, fetch_url: str) -> str:
    username, password = _proxy_auth_from_fetch_url(fetch_url)
    if not username and not password:
        return proxy_url
    parsed = urlparse(proxy_url)
    host = parsed.hostname or ""
    if not host:
        return proxy_url
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunparse(parsed._replace(netloc=f"{auth}{host}{port}"))


def _mask_proxy_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        if parsed.password:
            user = parsed.username or ""
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"{user}:***@{host}{port}" if user else f"***@{host}{port}"
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        logger.debug("failed to mask proxy url", exc_info=True)
    return value


def _is_rate_limited_text(value: Any) -> bool:
    text = str(value or "").lower()
    patterns = (
        "请求过于频繁",
        "操作过于频繁",
        "ip请求过于频繁",
        "ip非法请求过多",
        "非法请求过多",
        "已限制访问",
        "429",
        "too many requests",
        "rate limit",
    )
    return any(item.lower() in text for item in patterns)


def _is_ip_restricted_text(value: Any) -> bool:
    text = str(value or "").lower()
    patterns = ("ip非法请求过多", "非法请求过多", "已限制访问")
    return any(item.lower() in text for item in patterns)


def _ip_restrict_cooldown_seconds() -> float:
    return _parse_float_env(
        "MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS",
        DEFAULT_MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS,
        MAX_MOGUDING_IP_RESTRICT_COOLDOWN_SECONDS,
    )


def _mark_ip_restricted(reason: Any) -> None:
    global _IP_RESTRICTION_REASON, _IP_RESTRICTION_UNTIL
    message = str(reason or "IP非法请求过多，已限制访问").strip()
    cooldown = _ip_restrict_cooldown_seconds()
    with _IP_RESTRICTION_LOCK:
        _IP_RESTRICTION_REASON = message
        _IP_RESTRICTION_UNTIL = time.time() + cooldown
    logger.warning("工学云 IP 已受限，%.0f 秒内停止非代理请求: %s", cooldown, message)


def _current_ip_restriction_reason() -> str:
    global _IP_RESTRICTION_REASON, _IP_RESTRICTION_UNTIL
    with _IP_RESTRICTION_LOCK:
        if _IP_RESTRICTION_UNTIL <= time.time():
            _IP_RESTRICTION_REASON = ""
            _IP_RESTRICTION_UNTIL = 0.0
            return ""
        return _IP_RESTRICTION_REASON or "IP非法请求过多，已限制访问"


def _clear_ip_restriction_state() -> None:
    global _IP_RESTRICTION_REASON, _IP_RESTRICTION_UNTIL
    with _IP_RESTRICTION_LOCK:
        _IP_RESTRICTION_REASON = ""
        _IP_RESTRICTION_UNTIL = 0.0


class ApiClient:
    """
    ApiClient类用于与远程服务器进行交互，包括用户登录、获取实习计划、获取打卡信息、提交打卡等功能。
    """
    BASE_URL = "https://api.moguding.net:9000/"
    DEFAULT_HEADERS = {
        "user-agent": "Dart/3.7 (dart:io)",
        "content-type": "application/json; charset=utf-8",
        "accept-encoding": "gzip",
        "host": "api.moguding.net:9000",
    }

    @staticmethod
    def _is_token_expired_response(code, msg: str) -> bool:
        text = str(msg or "")
        if _is_ip_restricted_text(text):
            return False
        if code in (401, 7001, 7002):
            return True
        if code == 403:
            return text in {"token失效", "Token失效", "token已失效", "Token已失效", "登录已失效", "请重新登录"}
        return text in {"token失效", "Token失效", "token已失效", "Token已失效", "登录已失效", "请重新登录"}

    def __init__(self, config: ConfigManager):
        """
        初始化ApiClient实例。

        Args:
            config (ConfigManager): 用于管理配置的实例。
        """
        self.config = config
        self.max_retries = 5  # 控制重新尝试的次数
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)
        self._global_proxy_settings = self._load_global_proxy_settings()
        self._proxy_fetch_url = self._load_proxy_fetch_url()
        self._dynamic_proxy_url: Optional[str] = None
        self._dynamic_proxy_fetched_at = 0.0
        self._proxy_urls = self._load_proxy_urls()
        self._proxy_index = 0
        self._proxy_enabled = False
        if self._proxy_fetch_url:
            logger.info("已加载工学云动态补卡代理获取接口")
        if self._proxy_urls:
            logger.info("已加载工学云补卡代理池，共 %s 个代理", len(self._proxy_urls))

    def _load_global_proxy_settings(self) -> Dict[str, Any]:
        if any(
            str(os.getenv(name) or "").strip()
            for name in [
                "MOGUDING_PROXY_API_URL",
                "MOGUDING_PROXY_FETCH_URL",
                "MOGUDING_PROXY_URLS",
                "MOGUDING_PROXY_URL",
                "MOGUDING_PROXY",
            ]
        ):
            return {}
        tenant_id = "default"
        try:
            root = self.config.config if isinstance(self.config.config, dict) else {}
            tenant_id = str(root.get("tenant_id") or root.get("tenantId") or "default").strip() or "default"
        except Exception:
            tenant_id = "default"
        settings = load_global_proxy_settings(tenant_id=tenant_id)
        return settings if settings.get("enabled") else {}

    def _load_proxy_fetch_url(self) -> str:
        raw_values: List[Any] = [
            os.getenv("MOGUDING_PROXY_API_URL"),
            os.getenv("MOGUDING_PROXY_FETCH_URL"),
        ]
        try:
            root = self.config.config if isinstance(self.config.config, dict) else {}
            app_config = root.get("config") if isinstance(root.get("config"), dict) else {}
            network_config = app_config.get("network") if isinstance(app_config.get("network"), dict) else {}
            proxy_config = app_config.get("proxy") if isinstance(app_config.get("proxy"), dict) else {}
            raw_values.extend(
                [
                    network_config.get("proxyApiUrl"),
                    network_config.get("proxyFetchUrl"),
                    proxy_config.get("apiUrl"),
                    proxy_config.get("fetchUrl"),
                    app_config.get("proxyApiUrl"),
                    app_config.get("proxyFetchUrl"),
                ]
            )
        except Exception:
            logger.debug("failed to read configured proxy fetch url", exc_info=True)

        raw_values.append(self._global_proxy_settings.get("apiUrl"))
        for raw in raw_values:
            value = str(raw or "").strip()
            if not value:
                continue
            if _is_safe_proxy_fetch_url(value):
                return value
            logger.warning("已拒绝不安全的工学云代理获取接口: %s", _mask_proxy_url(value))
        return ""

    def _proxy_fetch_timeout_seconds(self) -> float:
        if self._global_proxy_settings.get("apiTimeoutSeconds") not in [None, ""]:
            return float(self._global_proxy_settings.get("apiTimeoutSeconds"))
        return _parse_float_env(
            "MOGUDING_PROXY_API_TIMEOUT_SECONDS",
            DEFAULT_MOGUDING_PROXY_API_TIMEOUT_SECONDS,
            MAX_MOGUDING_PROXY_API_TIMEOUT_SECONDS,
        )

    def _proxy_ttl_seconds(self) -> float:
        if self._global_proxy_settings.get("ttlSeconds") not in [None, ""]:
            return float(self._global_proxy_settings.get("ttlSeconds"))
        return _parse_float_env(
            "MOGUDING_PROXY_TTL_SECONDS",
            DEFAULT_MOGUDING_PROXY_TTL_SECONDS,
            MAX_MOGUDING_PROXY_TTL_SECONDS,
        )

    def _dynamic_proxy_expired(self) -> bool:
        if not self._dynamic_proxy_url:
            return True
        ttl = self._proxy_ttl_seconds()
        if ttl <= 0:
            return False
        return (time.time() - self._dynamic_proxy_fetched_at) >= ttl

    def _refresh_dynamic_proxy(self, reason: Optional[str] = None) -> bool:
        if not self._proxy_fetch_url:
            return False
        try:
            response = requests.get(self._proxy_fetch_url, timeout=self._proxy_fetch_timeout_seconds())
            response.raise_for_status()
            host_port = _extract_proxy_host_port(response.text)
            if not host_port:
                raise ValueError("代理接口未返回有效 ip:端口")
            proxy_url = _normalize_proxy_url(host_port)
            proxy_url = _apply_proxy_auth_from_fetch_url(proxy_url, self._proxy_fetch_url)
            old_proxy = self._dynamic_proxy_url
            self._dynamic_proxy_url = proxy_url
            self._dynamic_proxy_fetched_at = time.time()
            logger.warning(
                "已获取新的工学云动态代理: %s -> %s%s",
                _mask_proxy_url(old_proxy or ""),
                _mask_proxy_url(proxy_url),
                f"，原因: {reason}" if reason else "",
            )
            return True
        except Exception as exc:
            logger.warning("获取工学云动态代理失败: %s", exc)
            return False

    def _load_proxy_urls(self) -> List[str]:
        raw_values: List[Any] = [
            os.getenv("MOGUDING_PROXY_URLS"),
            os.getenv("MOGUDING_PROXY_URL"),
            os.getenv("MOGUDING_PROXY"),
        ]
        try:
            root = self.config.config if isinstance(self.config.config, dict) else {}
            app_config = root.get("config") if isinstance(root.get("config"), dict) else {}
            network_config = app_config.get("network") if isinstance(app_config.get("network"), dict) else {}
            proxy_config = app_config.get("proxy") if isinstance(app_config.get("proxy"), dict) else {}
            raw_values.extend(
                [
                    network_config.get("proxyUrls"),
                    network_config.get("proxyUrl"),
                    proxy_config.get("urls"),
                    proxy_config.get("url"),
                    app_config.get("proxyUrls"),
                    app_config.get("proxyUrl"),
                ]
            )
        except Exception:
            logger.debug("failed to read configured proxy url list", exc_info=True)
        raw_values.append(self._global_proxy_settings.get("proxyUrls"))

        urls: List[str] = []
        seen = set()
        for raw in raw_values:
            for item in _split_proxy_values(raw):
                proxy_url = _normalize_proxy_url(item)
                if proxy_url and proxy_url not in seen:
                    seen.add(proxy_url)
                    urls.append(proxy_url)
        return urls

    def enable_proxy(self) -> None:
        if not self._proxy_enabled:
            logger.info("已为本次手动补卡启用工学云代理")
        self._proxy_enabled = True

    def current_proxy_url(self) -> Optional[str]:
        if not self._proxy_enabled:
            return None
        if self._proxy_fetch_url:
            if self._dynamic_proxy_expired():
                self._refresh_dynamic_proxy(reason="初始化或代理过期")
            if self._dynamic_proxy_url:
                return self._dynamic_proxy_url
        if not self._proxy_urls:
            return None
        return self._proxy_urls[self._proxy_index % len(self._proxy_urls)]

    def _proxy_request_kwargs(self) -> Dict[str, Any]:
        proxy_url = self.current_proxy_url()
        if not proxy_url:
            return {}
        return {"proxies": {"http": proxy_url, "https": proxy_url}}

    def rotate_proxy(self, reason: Optional[str] = None) -> bool:
        if not self._proxy_enabled:
            return False
        if self._proxy_fetch_url:
            old_proxy = self._dynamic_proxy_url or ""
            if self._refresh_dynamic_proxy(reason=reason):
                new_proxy = self._dynamic_proxy_url or ""
                return bool(new_proxy) and new_proxy != old_proxy or bool(new_proxy)

        if len(self._proxy_urls) < 2:
            return False
        old_proxy = self.current_proxy_url()
        self._proxy_index = (self._proxy_index + 1) % len(self._proxy_urls)
        new_proxy = self.current_proxy_url()
        logger.warning(
            "工学云代理已切换: %s -> %s%s",
            _mask_proxy_url(old_proxy or ""),
            _mask_proxy_url(new_proxy or ""),
            f"，原因: {reason}" if reason else "",
        )
        return old_proxy != new_proxy

    def _post_request(
        self,
        url: str,
        headers: Dict[str, str],
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        发送POST请求，并处理请求过程中可能发生的错误。
        包括自动重试机制和Token失效处理。
        """
        full_url = f"{self.BASE_URL}{url}"
        
        for attempt in range(self.max_retries):
            try:
                proxy_kwargs = self._proxy_request_kwargs()
                using_proxy = bool(proxy_kwargs.get("proxies"))
                if not using_proxy:
                    restricted_reason = _current_ip_restriction_reason()
                    if restricted_reason:
                        raise ValueError(f"{restricted_reason}，已暂停非代理工学云请求")

                response = self.session.post(
                    full_url,
                    headers=headers,
                    json=data,
                    timeout=10,
                    **proxy_kwargs,
                )
                try:
                    rsp = response.json()
                except Exception:
                    rsp = {}
                status_code = getattr(response, "status_code", None)
                response_text = getattr(response, "text", "")
                msg_text = rsp.get("msg", response_text) if isinstance(rsp, dict) else response_text
                if status_code == 403 and _is_ip_restricted_text(msg_text or response_text):
                    if using_proxy:
                        if attempt < self.max_retries - 1 and self.rotate_proxy(reason=msg_text):
                            wait_time = 1 * (2 ** attempt)
                            logger.warning("代理 IP 已被工学云限制，已切换代理，等待 %.2f 秒后重试", wait_time)
                            time.sleep(wait_time)
                            continue
                    else:
                        _mark_ip_restricted(msg_text or response_text)
                    raise ValueError(msg_text or response_text)

                response.raise_for_status()
                
                code = rsp.get("code")
                msg = rsp.get("msg", "未知错误")

                # HTTP 成功不等于打卡业务成功，业务方法负责处理 302/304。
                if code == 200:
                    return rsp
                if code == 6111:
                    return rsp

                if _is_ip_restricted_text(msg):
                    if using_proxy:
                        if attempt < self.max_retries - 1 and self.rotate_proxy(reason=msg):
                            wait_time = 1 * (2 ** attempt)
                            logger.warning("代理 IP 已被工学云限制，已切换代理，等待 %.2f 秒后重试", wait_time)
                            time.sleep(wait_time)
                            continue
                    else:
                        _mark_ip_restricted(msg)
                    raise ValueError(msg)

                # Token 失效处理
                if self._is_token_expired_response(code, msg):
                    if attempt < self.max_retries - 1:
                        wait_time = 1 * (2 ** attempt)
                        logger.warning(f"Token失效，正在重新登录... (等待 {wait_time}s)")
                        time.sleep(wait_time)
                        
                        self.login()
                        # 更新请求头中的 authorization
                        headers["authorization"] = self.config.get_value("userInfo.token")
                        continue
                    else:
                        raise ValueError(msg)
                
                if _is_rate_limited_text(msg) and attempt < self.max_retries - 1 and self.rotate_proxy(reason=msg):
                    wait_time = 1 * (2 ** attempt)
                    logger.warning("请求触发频繁限制，已切换代理，等待 %.2f 秒后重试", wait_time)
                    time.sleep(wait_time)
                    continue

                raise ValueError(msg)

            except (requests.RequestException, ValueError) as e:
                # 如果是最后一次尝试，或者遇到无法重试的错误（如验证码），则抛出异常
                is_last_attempt = attempt >= self.max_retries - 1
                error_str = str(e)
                
                # 包含中文字符的错误通常是业务错误，或者已经达到最大重试次数
                if re.search(r"[\u4e00-\u9fff]", error_str) and "Token失效" not in error_str:
                     raise ValueError(error_str)
                
                if is_last_attempt:
                    raise ValueError(error_str)

                if isinstance(e, requests.RequestException):
                    self.rotate_proxy(reason=error_str)
                elif _is_rate_limited_text(error_str):
                    self.rotate_proxy(reason=error_str)

                wait_time = 1 * (2 ** attempt)
                logger.warning(f"请求失败: {e}，重试 {attempt + 1}/{self.max_retries}，等待 {wait_time:.2f} 秒")
                time.sleep(wait_time)
        
        raise ValueError("请求失败，超过最大重试次数")

    def pass_blockPuzzle_captcha(self, max_attempts: int = 5) -> str:
        """通过行为验证码（blockPuzzle）"""
        for attempt in range(max_attempts):
            try:
                captcha_url = "session/captcha/v1/get"
                request_data = {
                    "clientUid": str(uuid.uuid4()).replace("-", ""),
                    "captchaType": "blockPuzzle",
                }
                captcha_info = self._post_request(captcha_url, self.DEFAULT_HEADERS, request_data)
                
                slider_data = recognize_blockPuzzle_captcha(
                    captcha_info["data"]["jigsawImageBase64"],
                    captcha_info["data"]["originalImageBase64"],
                )
                
                check_slider_url = "session/captcha/v1/check"
                check_slider_data = {
                    "pointJson": aes_encrypt(slider_data, captcha_info["data"]["secretKey"], "b64"),
                    "token": captcha_info["data"]["token"],
                    "captchaType": "blockPuzzle",
                }
                
                check_result = self._post_request(check_slider_url, self.DEFAULT_HEADERS, check_slider_data)
                
                if check_result.get("code") != 6111:
                    return aes_encrypt(
                        captcha_info["data"]["token"] + "---" + slider_data,
                        captcha_info["data"]["secretKey"],
                        "b64",
                    )
            except Exception as e:
                logger.warning(f"滑块验证尝试 {attempt + 1}/{max_attempts} 失败: {e}")
                time.sleep(random.uniform(1, 3))
                
        raise Exception("通过滑块验证码失败")

    def solve_click_word_captcha(self, max_retries: int = 5) -> str:
        """通过点选验证码（clickWord）"""
        for retry in range(max_retries):
            try:
                captcha_endpoint = "/attendence/clock/v1/get"
                captcha_request_payload = {
                    "clientUid": str(uuid.uuid4()).replace("-", ""),
                    "captchaType": "clickWord",
                }

                captcha_response = self._post_request(
                    captcha_endpoint,
                    self._get_authenticated_headers(),
                    captcha_request_payload,
                )

                captcha_solution = recognize_clickWord_captcha(
                    captcha_response["data"]["originalImageBase64"],
                    captcha_response["data"]["wordList"],
                )

                verification_endpoint = "/attendence/clock/v1/check"
                verification_payload = {
                    "pointJson": aes_encrypt(captcha_solution, captcha_response["data"]["secretKey"], "b64"),
                    "token": captcha_response["data"]["token"],
                    "captchaType": "clickWord",
                }

                verification_response = self._post_request(
                    verification_endpoint,
                    self._get_authenticated_headers(),
                    verification_payload,
                )

                if verification_response.get("code") != 6111:
                    return aes_encrypt(
                        captcha_response["data"]["token"] + "---" + captcha_solution,
                        captcha_response["data"]["secretKey"],
                        "b64",
                    )
                
                time.sleep(random.uniform(1, 3))
            except Exception as e:
                logger.warning(f"点选验证尝试 {retry + 1}/{max_retries} 失败: {e}")
                time.sleep(random.uniform(1, 3))

        raise Exception("通过点选验证码失败")

    def login(self) -> None:
        """执行用户登录操作"""
        url = "session/user/v6/login"
        data = {
            "phone": aes_encrypt(self.config.get_value("config.user.phone")),
            "password": aes_encrypt(self.config.get_value("config.user.password")),
            "captcha": self.pass_blockPuzzle_captcha(),
            "loginType": "android",
            "uuid": str(uuid.uuid4()).replace("-", ""),
            "device": "android",
            "version": "5.16.0",
            "t": aes_encrypt(str(int(time.time() * 1000))),
        }
        rsp = self._post_request(url, self.DEFAULT_HEADERS, data)
        user_info = json.loads(aes_decrypt(rsp.get("data", "")))
        self.config.update_config(user_info, "userInfo")

    def fetch_internship_plan(self) -> None:
        """获取当前用户的实习计划"""
        url = "practice/plan/v3/getPlanByStu"
        data = {
            "pageSize": 999999,
            "t": aes_encrypt(str(int(time.time() * 1000)))
        }
        headers = self._get_authenticated_headers(sign_data=[
            self.config.get_value("userInfo.userId"),
            self.config.get_value("userInfo.roleKey"),
        ])
        rsp = self._post_request(url, headers, data)
        plan_info = rsp.get("data", [{}])[0]
        self.config.update_config(plan_info, "planInfo")

    def get_job_info(self) -> Dict[str, Any]:
        """获取用户的工作ID"""
        url = "practice/job/v4/infoByStu"
        data = {
            "planId": self.config.get_value("planInfo.planId"),
            "t": aes_encrypt(str(int(time.time() * 1000))),
        }
        headers = self._get_authenticated_headers()
        # 重试已经在 _post_request 中处理，这里只保留必要的特殊处理
        # 原来的 retry_count=3 针对 _post_request 递归，现在由 _post_request 内部统一重试
        rsp = self._post_request(url, headers, data)
        data = rsp.get("data", {})
        return {} if data is None else data

    def get_submitted_reports_info(self, report_type: str, page_size: int = 10, curr_page: int = 1) -> Dict[str, Any]:
        """获取已经提交的日报、周报或月报的数量"""
        url = "practice/paper/v2/listByStu"
        data = {
            "currPage": curr_page,
            "pageSize": page_size,
            "reportType": report_type,
            "planId": self.config.get_value("planInfo.planId"),
            "t": aes_encrypt(str(int(time.time() * 1000))),
        }
        headers = self._get_authenticated_headers(sign_data=[
            self.config.get_value("userInfo.userId"),
            self.config.get_value("userInfo.roleKey"),
            report_type,
        ])
        rsp = self._post_request(url, headers, data)
        return rsp

    def get_all_submitted_reports_info(self, report_type: str) -> Dict[str, Any]:
        """分页获取已经提交的全部日报、周报或月报。"""
        page_size = 100
        curr_page = 1
        merged: Dict[str, Any] = {}
        rows: List[Dict[str, Any]] = []
        while True:
            rsp = self.get_submitted_reports_info(report_type, page_size=page_size, curr_page=curr_page)
            if not merged:
                merged = dict(rsp or {})
            data = rsp.get("data", []) if isinstance(rsp, dict) else []
            if not isinstance(data, list) or not data:
                break
            rows.extend(data)
            if len(data) < page_size:
                break
            curr_page += 1
            if curr_page > 100:
                break
        merged["data"] = rows
        return merged

    def submit_report(self, report_info: Dict[str, Any]) -> None:
        """提交报告"""
        url = "practice/paper/v6/save"
        headers = self._get_authenticated_headers(sign_data=[
            self.config.get_value("userInfo.userId"),
            report_info.get("reportType"),
            self.config.get_value("planInfo.planId"),
            report_info.get("title"),
        ])
        
        # 使用 dict.fromkeys 将所有字段初始化为 None
        keys = [
            "address", "applyId", "applyName", "attachmentList", "commentNum",
            "commentContent", "content", "createBy", "createTime", "depName",
            "reject", "endTime", "headImg", "yearmonth", "imageList", "isFine",
            "latitude", "gpmsSchoolYear", "longitude", "planId", "planName",
            "reportId", "reportType", "reportTime", "isOnTime", "schoolId",
            "startTime", "state", "studentId", "studentNumber", "supportNum",
            "title", "url", "username", "weeks", "videoUrl", "videoTitle",
            "attachments", "companyName", "jobName", "jobId", "score",
            "tpJobId", "starNum", "confirmDays", "isApply", "compStarNum",
            "compScore", "compComment", "compState", "apply", "levelEntity",
            "formFieldDtoList", "fieldEntityList", "feedback", "handleWay",
            "isWarning", "warningType", "t"
        ]
        data = dict.fromkeys(keys, None)
        
        # 更新必要字段
        data.update({
            "content": report_info.get("content"),
            "planId": self.config.get_value("planInfo.planId"),
            "reportType": report_info.get("reportType"),
            "title": report_info.get("title"),
            "jobId": report_info.get("jobId", ""),
            "attachments": report_info.get("attachments", ""),
            "formFieldDtoList": report_info.get("formFieldDtoList", []),
            "fieldEntityList": report_info.get("formFieldDtoList", []),
            "isWarning": 0,
            "t": aes_encrypt(str(int(time.time() * 1000))),
        })
        
        # 更新可选字段（如果存在）
        for key in ["endTime", "startTime", "yearmonth", "reportTime", "weeks"]:
            if key in report_info:
                data[key] = report_info[key]

        self._post_request(url, headers, data)

    def get_weeks_date(self) -> List[Dict[str, Any]]:
        """获取本周周报周期信息"""
        url = "practice/paper/v3/getWeeks1"
        data = {"t": aes_encrypt(str(int(time.time() * 1000)))}
        headers = self._get_authenticated_headers()
        rsp = self._post_request(url, headers, data)
        return rsp.get("data", [])

    def get_from_info(self, formType: int) -> List[Dict[str, Any]]:
        """获取子表单（问卷），并设置值"""
        url = "practice/paper/v2/info"
        data = {
            "formType": formType,
            "t": aes_encrypt(str(int(time.time() * 1000)))
        }
        headers = self._get_authenticated_headers()
        rsp = self._post_request(url, headers, data).get("data", {})
        formFieldDtoList = rsp.get("formFieldDtoList", [])
        
        if not formFieldDtoList:
            return formFieldDtoList
            
        logger.info("检测到问卷，已自动填写")
        for item in formFieldDtoList:
            item["value"] = "b" # 默认选择 B
            
        return formFieldDtoList

    @staticmethod
    def _coerce_clockin_range_time(value: Any, *, end: bool = False) -> str:
        if isinstance(value, datetime.datetime):
            dt = value
        elif isinstance(value, datetime.date):
            dt = datetime.datetime.combine(value, datetime.time())
        else:
            raw = str(value or "").strip().replace("/", "-")
            parsed = None
            for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
                try:
                    parsed = datetime.datetime.strptime(raw[:size], fmt)
                    break
                except Exception:
                    continue
            dt = parsed or datetime.datetime.now()
        if end:
            return dt.strftime("%Y-%m-%d 00:00:00Z")
        return dt.strftime("%Y-%m-%d 00:00:00")

    def get_checkin_records(
        self,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """获取指定时间范围内的打卡记录。未传范围时默认获取当前月。"""
        url = "attendence/clock/v2/listSynchro"
        if self.config.get_value("userInfo.userType") == "teacher":
            url = "attendence/clock/teacher/v1/listSynchro"
            
        headers = self._get_authenticated_headers()
        data = get_current_month_info()
        if start_time is not None and end_time is not None:
            data = {
                "startTime": self._coerce_clockin_range_time(start_time),
                "endTime": self._coerce_clockin_range_time(end_time, end=True),
            }
        data["t"] = aes_encrypt(str(int(time.time() * 1000)))
        rsp = self._post_request(url, headers, data)
        rows = rsp.get("data", []) if isinstance(rsp, dict) else []
        return rows if isinstance(rows, list) else []

    def get_checkin_info(self) -> Dict[str, Any]:
        """获取用户最近一条打卡信息"""
        rows = self.get_checkin_records()
        return rows[0] if rows else {}

    def _clock_in_sign_data(self, checkin_info: Dict[str, Any], plan_id: Any) -> Optional[List[str]]:
        if self.config.get_value("userInfo.userType") == "teacher":
            return None

        device = self.config.get_value("config.device")
        user_id = self.config.get_value("userInfo.userId")
        location = self.config.get_value("config.clockIn.location") or {}
        if not isinstance(location, dict):
            location = {}
        address = self.config.get_value("config.clockIn.location.address") or location.get("address")
        missing = []
        if not device:
            missing.append("device")
        if not checkin_info.get("type"):
            missing.append("type")
        if not plan_id:
            missing.append("planId")
        if not user_id:
            missing.append("userId")
        if not address:
            missing.append("clockIn.location.address")
        if missing:
            raise ValueError("打卡签名必填字段缺失: " + ", ".join(missing))
        return [
            device,
            str(checkin_info.get("type")),
            plan_id,
            user_id,
            str(address),
        ]

    def create_alipay_clockin_verification(self) -> Dict[str, str]:
        """创建支付宝打卡安全验证登记。"""
        response = self._post_request(
            "usercenter/alipay/v1/createAxdjk",
            self._get_authenticated_headers(),
            {"t": aes_encrypt(str(int(time.time() * 1000)))},
        )
        data = response.get("data") if isinstance(response, dict) else None
        if (
            not isinstance(response, dict)
            or response.get("code") != 200
            or response.get("msg") != "success"
            or not isinstance(data, dict)
        ):
            raise ValueError("创建支付宝安全验证失败")

        out_register_no = data.get("outRegisterNo")
        register_url = data.get("registerUrl")
        if not isinstance(out_register_no, str) or not isinstance(register_url, str):
            raise ValueError("支付宝安全验证响应不完整")

        out_register_no = out_register_no.strip()
        register_url = register_url.strip()
        if not out_register_no or not register_url:
            raise ValueError("支付宝安全验证响应不完整")
        if (
            not register_url.startswith("alipays://")
            or urlparse(register_url).scheme.lower() != "alipays"
        ):
            raise ValueError("支付宝安全验证链接无效")
        return {
            "outRegisterNo": out_register_no,
            "registerUrl": register_url,
        }

    def _submit_clock_in_payload(
        self,
        checkin_info: Dict[str, Any],
        *,
        replace: bool = False,
    ) -> Dict[str, str]:
        """提交普通打卡或补卡请求。"""
        url = "attendence/clock/teacher/v2/save"
        planId = self.config.get_value("planInfo.planId")
        sign_data = self._clock_in_sign_data(checkin_info, planId)

        if self.config.get_value("userInfo.userType") != "teacher":
            url = "attendence/attendanceReplace/v6/save" if replace else "attendence/clock/v6/save"

        logger.info(f'打卡类型：{checkin_info.get("type")}')
        
        # 将所有可能的字段初始化为 None
        keys = [
            "distance", "content", "lastAddress", "lastDetailAddress", "attendanceId", 
            "country", "createBy", "createTime", "description", "device", "images", 
            "isDeleted", "isReplace", "modifiedBy", "modifiedTime", "schoolId", 
            "state", "teacherId", "teacherNumber", "type", "captcha", "appUuid", "isFirstSignInWeb", "stuId", "planId",
            "attendanceType", "username", "isBeyondFenceStu", "attachments", "userId", "isSYN",
            "studentId", "applyState", "studentNumber", "memberNumber", "headImg", 
            "attendenceTime", "depName", "majorName", "className", "logDtoList", 
            "isBeyondFence", "practiceAddress", "tpJobId","jobAddress","jobAddrPoint","certifyId","outRegisterNo","t"
        ]
        data = dict.fromkeys(keys, None)

        clockin_time = (
            checkin_info.get("createTime")
            or checkin_info.get("attendenceTime")
            or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        )

        data.update({
            "country": "中国",
            "createTime": clockin_time,
            "description": checkin_info.get("description", None),
            "device": self.config.get_value("config.device"),
            "isReplace": None if replace else checkin_info.get("isReplace", None),
            "state": "NORMAL",
            "type": checkin_info.get("type"),
            "planId": planId,
            "attendanceType": "REPLACE" if replace else checkin_info.get("attendanceType", None),
            "attachments": checkin_info.get("attachments", None),
            "userId": self.config.get_value("userInfo.userId"),
            # The v6 endpoint rejects the legacy lastDetailAddress field.
            "attendenceTime": None if replace else clockin_time,
            "outRegisterNo": checkin_info.get("outRegisterNo"),
            "t": aes_encrypt(str(int(time.time() * 1000))),
        })

        location2 = self.config.get_value("config.clockIn.location") or {}
        if not isinstance(location2, dict):
            location2 = {}
        data.update(location2)

        headers = self._get_authenticated_headers(sign_data)
        if replace and self.config.get_value("userInfo.userType") != "teacher":
            headers["user-agent"] = "Dart/3.7 (dart:io)"
            headers["content-type"] = "application/json"
        response = self._post_request(url, headers, data)
        if response.get("msg") == "302":
            logger.info("检测到行为验证码，正在通过···")
            data["captcha"] = self.solve_click_word_captcha()
            response = self._post_request(url, headers, data)
        if response.get("msg") == "302":
            raise ValueError("打卡失败，行为验证码未通过")
        if response.get("msg") == "304":
            return {"status": "verification_required"}
        return {"status": "success"}

    def submit_clock_in(self, checkin_info: Dict[str, Any]) -> Dict[str, str]:
        """提交打卡信息"""
        return self._submit_clock_in_payload(checkin_info, replace=False)

    def submit_clock_in_replace(self, checkin_info: Dict[str, Any]) -> Dict[str, str]:
        """提交补卡信息"""
        return self._submit_clock_in_payload(checkin_info, replace=True)

    def get_upload_token(self) -> str:
        """获取上传文件的认证令牌"""
        url = "session/upload/v1/token"
        headers = self._get_authenticated_headers()
        data = {"t": aes_encrypt(str(int(time.time() * 1000)))}
        rsp = self._post_request(url, headers, data)
        return rsp.get("data", "")

    def _get_authenticated_headers(
        self,
        sign_data: Optional[List[Optional[str]]] = None
    ) -> Dict[str, str]:
        """生成带有认证信息的请求头"""
        headers = self.DEFAULT_HEADERS.copy()
        headers.update({
            "authorization": self.config.get_value("userInfo.token"),
            "userid": self.config.get_value("userInfo.userId"),
            "rolekey": self.config.get_value("userInfo.roleKey"),
        })
        
        if sign_data:
            headers["sign"] = create_sign(*sign_data)
        return headers
