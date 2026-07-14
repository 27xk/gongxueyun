import logging
import smtplib
import threading
from collections import Counter
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, List

import requests

from server.user_runtime import sanitize_execution_results

try:
    from main import _log_ctx
except ImportError:
    _log_ctx = threading.local()

logger = logging.getLogger(__name__)


class MessagePusher:
    STATUS_EMOJIS = {"success": "✅", "fail": "❌", "skip": "⏭️", "unknown": "❓"}
    SUPPORTED_TYPES = {"Server", "SMTP"}
    SMTP_HOST = "smtp.qq.com"
    SMTP_PORT = 465

    def __init__(self, push_config: list | None):
        self.push_config = push_config or []

    def push(self, results: List[Dict[str, Any]]) -> None:
        skip_count = sum(1 for result in results if result.get("status") == "skip")
        if results and skip_count == len(results):
            logger.info("所有任务均被跳过，不发送推送消息")
            return

        success_count = sum(result.get("status") == "success" for result in results)
        title = f"{'🎉' if success_count == len(results) else '📋'} 工学云报告({success_count}/{len(results)})"

        for service_config in self.push_config:
            if not isinstance(service_config, dict):
                continue
            service_type = str(service_config.get("type") or "").strip()
            if service_type not in self.SUPPORTED_TYPES:
                continue
            if not service_config.get("enabled", False):
                continue
            try:
                if service_type == "Server":
                    self._server_push(service_config, title, self._generate_markdown_message(results))
                elif service_type == "SMTP":
                    self._smtp_push(service_config, title, self._generate_html_message(results))
            except Exception as exc:
                logger.error("%s 消息推送失败: %s", service_type, exc)

    def _server_push(self, config: Dict[str, Any], title: str, content: str) -> None:
        send_key = str(config.get("sendKey") or "").strip()
        if not send_key:
            raise ValueError("Server 酱 sendKey 不能为空")
        url = f"https://sctapi.ftqq.com/{send_key}.send"
        rsp = requests.post(url, data={"title": title, "desp": content}, timeout=15).json()
        if rsp.get("code") != 0:
            raise RuntimeError(rsp.get("message") or "Server 酱推送失败")
        logger.info("Server 酱推送成功")

    def _smtp_push(self, config: Dict[str, Any], title: str, content: str) -> None:
        username = str(config.get("username") or "").strip()
        password = str(config.get("password") or "").strip()
        to = str(config.get("to") or "").strip()
        from_name = str(config.get("from") or "工学云签到通知").strip()
        if not username or not password or not to:
            raise ValueError("SMTP 配置缺少 username/password/to")

        msg = MIMEMultipart()
        msg["From"] = formataddr((Header(from_name, "utf-8").encode(), username))
        msg["To"] = to
        msg["Subject"] = Header(title, "utf-8").encode()
        msg.attach(MIMEText(content, "html", "utf-8"))

        with smtplib.SMTP_SSL(self.SMTP_HOST, self.SMTP_PORT) as server:
            server.login(username, password)
            server.send_message(msg)
        logger.info("SMTP 推送成功")

    @classmethod
    def _generate_markdown_message(cls, results: List[Dict[str, Any]]) -> str:
        results = sanitize_execution_results(results)
        status_counts = Counter(result.get("status", "unknown") for result in results)
        total_tasks = len(results)
        parts = [
            "# 工学云任务执行报告\n\n",
            "## 执行统计\n\n",
            f"- 总任务数：{total_tasks}\n",
            f"- 成功：{status_counts['success']}\n",
            f"- 失败：{status_counts['fail']}\n",
            f"- 跳过：{status_counts['skip']}\n\n",
            "## 详细任务报告\n\n",
        ]
        for result in results:
            task_type = result.get("task_type", "未知任务")
            status = result.get("status", "unknown")
            status_emoji = cls.STATUS_EMOJIS.get(status, cls.STATUS_EMOJIS["unknown"])
            parts.extend(
                [
                    f"### {status_emoji} {task_type}\n\n",
                    f"**状态：** {status}\n\n",
                    f"**结果：** {result.get('message', '无消息')}\n\n",
                ]
            )
            details = result.get("details")
            if isinstance(details, dict) and details:
                parts.append("**详细信息：**\n\n")
                for key, value in details.items():
                    parts.append(f"- **{key}**：{value}\n")
                parts.append("\n")
            report_content = result.get("report_content")
            if isinstance(report_content, str) and report_content.strip():
                parts.extend(["**报告：**\n\n", f"```\n{report_content}\n```\n\n"])
            parts.append("---\n\n")
        return "".join(parts)

    @classmethod
    def _generate_html_message(cls, results: List[Dict[str, Any]]) -> str:
        results = sanitize_execution_results(results)
        status_counts = Counter(result.get("status", "unknown") for result in results)
        total_tasks = len(results)
        html = [
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
            "<title>工学云任务执行报告</title>",
            '<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.6;padding:20px;color:#1f2328;}h1{margin-bottom:16px;}h2{margin-top:24px;}pre{white-space:pre-wrap;background:#f6f8fa;padding:12px;border-radius:6px;}.card{border:1px solid #d0d7de;border-radius:8px;padding:16px;margin-bottom:16px;}.muted{color:#57606a;}</style>',
            "</head><body>",
            "<h1>工学云任务执行报告</h1>",
            f'<p class="muted">总任务数：{total_tasks} / 成功：{status_counts["success"]} / 失败：{status_counts["fail"]} / 跳过：{status_counts["skip"]}</p>',
            "<h2>详细任务报告</h2>",
        ]
        for result in results:
            task_type = result.get("task_type", "未知任务")
            status = result.get("status", "unknown")
            html.append('<div class="card">')
            html.append(f'<h3>{cls.STATUS_EMOJIS.get(status, cls.STATUS_EMOJIS["unknown"])} {task_type}</h3>')
            html.append(f'<p><strong>状态：</strong>{status}</p>')
            html.append(f'<p><strong>结果：</strong>{result.get("message", "无消息")}</p>')
            details = result.get("details")
            if isinstance(details, dict) and details:
                html.append("<ul>")
                for key, value in details.items():
                    html.append(f"<li><strong>{key}：</strong>{value}</li>")
                html.append("</ul>")
            report_content = result.get("report_content")
            if isinstance(report_content, str) and report_content.strip():
                html.append(f"<pre>{report_content}</pre>")
            html.append("</div>")
        html.append("</body></html>")
        return "".join(html)


def send_test_smtp_message(config: Dict[str, Any]) -> str:
    smtp_config = dict(config or {})
    username = str(smtp_config.get("username") or "").strip()
    smtp_config["to"] = username
    title = "工学云 SMTP 测试邮件"
    content = "<p>这是一封 SMTP 测试邮件，说明当前 QQ 邮箱 SMTP 配置可用。</p>"
    MessagePusher([])._smtp_push(smtp_config, title, content)
    return username
