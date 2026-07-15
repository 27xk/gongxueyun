import contextvars
import fnmatch
import os
import time
import ipaddress
import logging
import socket
import threading
from contextlib import contextmanager
from typing import Dict, Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from requests.exceptions import RequestException

from server.util.HelperFunctions import strip_markdown

logger = logging.getLogger(__name__)
DEFAULT_AI_PROMPT_VERSION = "2026-05-29.1"
_ORIGINAL_GETADDRINFO = getattr(socket.getaddrinfo, "_automoguding_original", socket.getaddrinfo)
_PINNED_GETADDRINFO_CONTEXT = contextvars.ContextVar("automoguding_ai_pinned_getaddrinfo", default=None)
_GETADDRINFO_PATCH_LOCK = threading.RLock()
_GETADDRINFO_PATCH_DEPTH = 0
_GETADDRINFO_PATCH_PREVIOUS = None


def _getaddrinfo_with_context_pin(query_host, query_port, *args, **kwargs):
    pinned = _PINNED_GETADDRINFO_CONTEXT.get()
    if pinned:
        pinned_host, pinned_port, pinned_infos = pinned
        try:
            query_port_int = int(query_port or pinned_port)
        except Exception:
            query_port_int = pinned_port
        if (
            str(query_host or "").strip().lower() == pinned_host
            and query_port_int == pinned_port
            and pinned_infos
        ):
            return list(pinned_infos)
    return _ORIGINAL_GETADDRINFO(query_host, query_port, *args, **kwargs)


_getaddrinfo_with_context_pin._automoguding_original = _ORIGINAL_GETADDRINFO


@contextmanager
def _patched_getaddrinfo():
    global _GETADDRINFO_PATCH_DEPTH, _GETADDRINFO_PATCH_PREVIOUS
    with _GETADDRINFO_PATCH_LOCK:
        if _GETADDRINFO_PATCH_DEPTH == 0:
            _GETADDRINFO_PATCH_PREVIOUS = socket.getaddrinfo
            socket.getaddrinfo = _getaddrinfo_with_context_pin
        _GETADDRINFO_PATCH_DEPTH += 1
    try:
        yield
    finally:
        with _GETADDRINFO_PATCH_LOCK:
            _GETADDRINFO_PATCH_DEPTH = max(0, _GETADDRINFO_PATCH_DEPTH - 1)
            if _GETADDRINFO_PATCH_DEPTH == 0 and _GETADDRINFO_PATCH_PREVIOUS is not None:
                socket.getaddrinfo = _GETADDRINFO_PATCH_PREVIOUS
                _GETADDRINFO_PATCH_PREVIOUS = None

def _resolve_chat_completions_url(api_base_url: str) -> str:
    base = (api_base_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        return urljoin(base + "/", "chat/completions")
    return urljoin(base + "/", "v1/chat/completions")


def _split_env_list(value: str) -> list[str]:
    return [item.strip().lower() for item in (value or "").replace(";", ",").split(",") if item.strip()]


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _host_matches_allowlist(host: str, allowed_hosts: list[str]) -> bool:
    if not allowed_hosts:
        return True
    if "*" in allowed_hosts or host in allowed_hosts:
        return True
    return any(item.startswith(".") and host.endswith(item) for item in allowed_hosts)


def get_ai_prompt_version() -> str:
    return (os.getenv("AI_PROMPT_VERSION") or DEFAULT_AI_PROMPT_VERSION).strip() or DEFAULT_AI_PROMPT_VERSION


def _validate_ai_model_policy(model: str) -> str:
    model_name = str(model or "").strip()
    if not model_name:
        raise ValueError("AI model is required")
    allowed_models = _split_env_list(os.getenv("AI_ALLOWED_MODELS") or "")
    if not allowed_models:
        return model_name
    lowered = model_name.lower()
    if any(item == "*" or fnmatch.fnmatchcase(lowered, item) for item in allowed_models):
        return model_name
    raise ValueError("AI model is not in AI_ALLOWED_MODELS")


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


def _resolve_host_for_endpoint_policy(host: str, port: int) -> tuple[bool, list]:
    normalized = (host or "").strip().lower()
    if not normalized or normalized == "localhost":
        return True, []
    try:
        ipaddress.ip_address(normalized)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    except Exception:
        return True, []

    if is_ip_literal:
        infos = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
        return _is_private_or_special_ip(normalized), infos
    try:
        infos = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
    except Exception:
        return True, []
    return any(_is_private_or_special_ip(info[4][0]) for info in infos), list(infos)


@contextmanager
def _pin_getaddrinfo(host: str, port: int, infos: list):
    normalized_host = (host or "").strip().lower()
    normalized_port = int(port)
    token = _PINNED_GETADDRINFO_CONTEXT.set((normalized_host, normalized_port, list(infos or [])))
    try:
        with _patched_getaddrinfo():
            yield
    finally:
        _PINNED_GETADDRINFO_CONTEXT.reset(token)


def _ai_endpoint_detail(api_url: str) -> Dict[str, Any]:
    parsed = urlparse((api_url or "").strip())
    return {
        "scheme": parsed.scheme,
        "host": (parsed.hostname or "").lower(),
        "port": parsed.port,
        "path": parsed.path,
    }


def _validate_ai_endpoint_policy(api_url: str) -> Dict[str, Any]:
    detail = _ai_endpoint_detail(api_url)
    host = detail["host"]
    if not detail["scheme"] or not host:
        raise ValueError("AI API URL format is invalid")

    allowed_hosts = _split_env_list(os.getenv("AI_ALLOWED_HOSTS") or "")
    if not _host_matches_allowlist(host, allowed_hosts):
        raise ValueError("AI API URL is not in AI_ALLOWED_HOSTS")

    allow_private = _env_flag("ALLOW_PRIVATE_AI_ENDPOINTS")
    port = int(detail.get("port") or (443 if detail["scheme"] == "https" else 80))
    private_endpoint, resolved_infos = _resolve_host_for_endpoint_policy(host, port)
    if private_endpoint:
        if not allow_private:
            raise ValueError("AI API URL must not target private, local, or special addresses")
        if not allowed_hosts:
            raise ValueError("AI private endpoint requires AI_ALLOWED_HOSTS")
        detail["_resolved_infos"] = resolved_infos
        return detail

    if detail["scheme"] != "https":
        raise ValueError("AI API URL must use HTTPS for public endpoints")
    detail["_resolved_infos"] = resolved_infos
    return detail

def _clamp_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        v = int(value)
    except Exception:
        v = default
    return max(min_value, min(max_value, v))

def _truncate_to_chars(text: str, max_chars: int) -> str:
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _ai_timeout_seconds(value: Any) -> int:
    max_timeout = _clamp_int(os.getenv("AI_REQUEST_MAX_TIMEOUT_SECONDS"), default=60, min_value=1, max_value=60)
    return _clamp_int(value, default=min(30, max_timeout), min_value=1, max_value=max_timeout)


def _ai_max_retries(value: Any) -> int:
    configured_max = _clamp_int(os.getenv("AI_REQUEST_MAX_RETRIES"), default=2, min_value=1, max_value=3)
    return _clamp_int(value, default=configured_max, min_value=1, max_value=configured_max)


def _ai_max_output_tokens() -> int:
    return _clamp_int(os.getenv("AI_MAX_OUTPUT_TOKENS"), default=1200, min_value=256, max_value=4000)


def _submitted_report_prompts(submitted_reports: Optional[list]) -> str:
    if not isinstance(submitted_reports, list):
        return ""
    max_items = _clamp_int(os.getenv("AI_SUBMITTED_REPORT_HISTORY_LIMIT"), default=8, min_value=0, max_value=20)
    max_chars = _clamp_int(os.getenv("AI_SUBMITTED_REPORT_HISTORY_CHARS"), default=4000, min_value=0, max_value=12000)
    if max_items <= 0 or max_chars <= 0:
        return ""
    prompts = []
    used = 0
    for report in submitted_reports[:max_items]:
        if not isinstance(report, dict):
            continue
        item = f"{str(report.get('title') or '')[:120]}: {str(report.get('content') or '')[:1000]}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        item = item[:remaining]
        prompts.append(item)
        used += len(item)
    return "\n".join(prompts)


def generate_article(
    config: Any,
    title: str,
    job_info: Dict[str, Any],
    count: int = 350,
    submitted_reports: Optional[list] = None,
    max_retries: int = 3,
    retry_delay: int = 1,
    timeout: int = 600,
) -> str:
    """
    生成日报、周报、月报。

    Args:
        config: 配置管理器，负责提供 API 配置。
        title: 文章标题。
        job_info: 工作相关信息字典。
        count: 字数下限，默认500。
        max_retries: 最大重试次数，默认3。
        retry_delay: 每次重试的延迟时间（秒）。
        timeout: 请求超时时间（秒）。
    Returns:
        生成的文章内容字符串。
    Raises:
        ValueError: 超过最大重试、响应异常、内容异常。
    """

    # 获取所有配置，仅调用一次
    api_key = config.get_value("config.ai.apikey")
    api_base_url = config.get_value("config.ai.apiUrl")
    api_model = _validate_ai_model_policy(config.get_value("config.ai.model"))

    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    api_url = _resolve_chat_completions_url(api_base_url)
    endpoint_detail = _validate_ai_endpoint_policy(api_url)
    resolved_infos = endpoint_detail.pop("_resolved_infos", [])
    endpoint_host = str(endpoint_detail.get("host") or "")
    endpoint_port = int(endpoint_detail.get("port") or (443 if endpoint_detail.get("scheme") == "https" else 80))
    logger.info("AI endpoint selected: %s", endpoint_detail)

    min_count = _clamp_int(count, default=500, min_value=1, max_value=1000)
    max_chars = 1000
    request_timeout = _ai_timeout_seconds(timeout)
    request_retries = _ai_max_retries(max_retries)

    # 构造系统提示词
    system_prompt = (
        f"根据用户提供的信息撰写一篇文章，内容流畅且符合中文语法规范，"
        f"不得使用 Markdown 语法，字数不少于 {min_count} 字，且不得超过 {max_chars} 字。"
        f"根据给出的历史日报生成周报，周报生成月报，如果是给出的周报生成周报则不能出现重复内容"
        f"文章需与职位描述相关，并使用以下模板："
        "\n\n模板：\n实习地点：xxxx\n\n工作内容：\n\nxxxxxx\n\n工作总结：\n\nxxxxxx\n\n"
        "遇到问题：\n\nxxxxxx\n\n自我评价：\n\nxxxxxx")

    # 提取公司信息，保证对象安全
    system_prompt = f"Prompt-Version: {get_ai_prompt_version()}\n" + system_prompt
    company_info = job_info.get("practiceCompanyEntity", {}) or {}
    majorName = config.get_value('userInfo.orgJson.majorName')
    data = {
        "model":
        api_model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role":
                "user",
                "content":
                (f"相关资料：报告标题：{title}，"
                 f"学生专业：{majorName}; "
                 f"工作地点：{job_info.get('jobAddress', '未知')}; "
                 f"公司名：{company_info.get('companyName', '未知')}; "
                 f"岗位职责：{job_info.get('quartersIntroduce', '未提供')}; "
                 f"公司所属行业：{company_info.get('tradeValue', '未提供')}"),
            },
        ],
        "max_tokens": _ai_max_output_tokens(),
    }
    prompts = _submitted_report_prompts(submitted_reports)
    if prompts:
        data["messages"].append({
            "role": "user",
            "content": prompts
        })

    def parse_response(resp_json: Dict) -> Optional[str]:
        """
        从接口响应解析content，返回None表示解析失败。
        """
        try:
            choices = resp_json.get("choices")
            if not choices or not isinstance(choices, list):
                return None
            return choices[0].get("message", {}).get("content",
                                                     "").strip() or None
        except Exception:
            logger.exception("解析响应发生异常")
            return None

    # === 主重试流程 ===
    for attempt in range(1, request_retries + 1):
        try:
            logger.info(f"第 {attempt} 次请求，标题：{title}")
            with _pin_getaddrinfo(endpoint_host, endpoint_port, resolved_infos):
                response = requests.post(
                    url=api_url,
                    headers=headers,
                    json=data,
                    timeout=request_timeout,
                )
            response.raise_for_status()
            content = parse_response(response.json())
            if not content:
                logger.error("AI 返回内容为空或格式不正确")
                raise ValueError("AI 返回内容为空或格式不正确")
            logger.info("文章生成成功")
            cleaned = strip_markdown(content)
            return _truncate_to_chars(cleaned, max_chars)
        except RequestException as e:
            logger.warning("网络请求错误（尝试 %s/%s）：%s", attempt, request_retries, e.__class__.__name__)
            if attempt == request_retries:
                logger.error("达到最大重试次数，最后一次错误类型: %s", e.__class__.__name__)
                raise ValueError("网络异常，生成失败")
            time.sleep(retry_delay)
        except ValueError as e:
            logger.error(f"内容错误或解析失败：{e}")
            raise
        except Exception:
            logger.exception("未知异常（第 %s 次）", attempt)
            if attempt == request_retries:
                raise ValueError("生成文章失败，未知错误")
            time.sleep(retry_delay)

    raise ValueError("文章生成失败，所有重试均未成功")
