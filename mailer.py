"""
邮件发送模块
通过阿里云邮件推送 (SMTP) 发送 HTML 格式的抖音视频要闻邮件。
收件人由 emails.csv 管理 (email, expire_date 两列)。
从 twitter_stock_monitor/mailer.py 复用核心 SMTP 逻辑。
"""

import csv
import logging
import os
import smtplib
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import List, Optional

logger = logging.getLogger(__name__)


class Mailer:
    """邮件发送器 (阿里云邮件推送)"""

    def __init__(self, config: dict):
        self.host: str = config.get("host", "smtpdm.aliyun.com")
        self.port: int = config.get("port", 465)
        self.use_ssl: bool = config.get("use_ssl", True)
        self.username: str = config.get("username", "")
        self.password: str = config.get("password", "")
        self.from_name: str = config.get("from_name", "模型先生视频提醒")

    def send_video_alert(
        self,
        to_addrs: List[str],
        video_info: dict,
        analysis: dict,
    ) -> bool:
        """发送视频要闻邮件给多位收件人。

        Args:
            to_addrs: 收件人邮箱列表
            video_info: 视频信息 dict (video_id, title, url, cover_url, publish_time, author_name)
            analysis: LLM 分析结果 dict (summary, sectors, stocks_mentioned)

        Returns:
            bool: 是否至少有一封发送成功
        """
        if not to_addrs:
            logger.warning("收件人列表为空，跳过发送")
            return False

        now_str = datetime.now().strftime("%m-%d %H:%M")
        # 邮件标题
        summary_short = analysis.get("summary", "")[:40]
        if summary_short:
            subject = f"[模型先生] {summary_short}... - {now_str}"
        else:
            subject = f"[模型先生] 最新视频要点 - {now_str}"

        html = self._build_html(video_info, analysis)

        n = len(to_addrs)
        pool_size = max(1, min(n // 5 + (1 if n % 5 else 0), 15))
        logger.info(f"开始并行发送 {n} 封邮件 (线程池: {pool_size})")

        success, fail = 0, 0
        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = {
                executor.submit(self._send_one, addr, subject, html): addr
                for addr in to_addrs
            }
            for fut in as_completed(futures):
                addr = futures[fut]
                try:
                    ok = fut.result()
                    if ok:
                        success += 1
                    else:
                        fail += 1
                except Exception as e:
                    logger.error(f"线程执行异常 ({addr}): {e}")
                    fail += 1

        logger.info(f"邮件发送完毕: 成功={success} 失败={fail}")
        return success > 0

    def _send_one(self, to_addr: str, subject: str, html: str) -> bool:
        """发送单封邮件"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        from_header = formataddr((self.from_name, self.username))
        msg["From"] = from_header
        msg["To"] = to_addr
        msg.attach(MIMEText(html, "html", "utf-8"))

        for attempt in range(3):
            try:
                if self.use_ssl:
                    context = ssl.create_default_context()
                    with smtplib.SMTP_SSL(
                        self.host, self.port, timeout=15, context=context
                    ) as server:
                        server.login(self.username, self.password)
                        server.sendmail(self.username, [to_addr], msg.as_string())
                else:
                    with smtplib.SMTP(self.host, self.port, timeout=15) as server:
                        server.ehlo()
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                        server.login(self.username, self.password)
                        server.sendmail(self.username, [to_addr], msg.as_string())

                logger.info(f"已发送 → {to_addr}")
                return True

            except smtplib.SMTPAuthenticationError:
                logger.error(f"SMTP 认证失败，请检查密码: {to_addr}")
                return False
            except Exception as e:
                err_msg = str(e)
                # 频控退避
                if (
                    "too many" in err_msg.lower()
                    or "limit" in err_msg.lower()
                    or "421" in err_msg
                ):
                    wait = 5 * (attempt + 1)
                else:
                    wait = 2

                logger.warning(
                    f"发送异常 {to_addr} ({err_msg[:40]})，{wait}s 后重试 ({attempt + 1}/3)"
                )
                if attempt < 2:
                    time.sleep(wait)

        logger.error(f"发送失败 (已重试3次): {to_addr}")
        return False

    def _build_html(self, video_info: dict, analysis: dict) -> str:
        """构建 HTML 邮件内容"""
        video_title = video_info.get("title", "")
        video_url = video_info.get("url", "")
        video_author = video_info.get("author_name", "模型先生")
        cover_url = video_info.get("cover_url", "")
        original_text = video_info.get("original_text", "")
        text_source = video_info.get("text_source", "none")

        summary = analysis.get("summary", "")
        sectors = analysis.get("sectors", [])
        stocks_mentioned = analysis.get("stocks_mentioned", [])

        # ── 安全转义 ──
        def _escape(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        safe_title = _escape(video_title)
        safe_summary = _escape(summary)

        # ── 原文区块（邮件最上方，最重要）──
        original_block = ""
        if original_text:
            safe_original = _escape(original_text)
            if text_source == "subtitle":
                source_label = "📺 视频字幕原文"
            elif text_source == "whisper":
                source_label = "🎙️ 语音转文字原文"
            else:
                source_label = "📝 视频原文"

            original_block = f"""
          <div style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
            <h3 style="margin:0 0 10px;color:#333;font-size:16px;">{source_label}</h3>
            <blockquote style="margin:0;padding:16px 18px;background:#fffdf5;border-left:4px solid #faad14;border-radius:4px;line-height:1.9;color:#444;white-space:pre-wrap;word-break:break-word;font-size:14px;">
{safe_original}
            </blockquote>
          </div>"""

        # ── 板块推荐区块 ──
        sector_block = ""
        if sectors:
            sector_items = ""
            for sec in sectors:
                name = sec.get("name", "")
                reason = sec.get("reason", "")
                stocks = sec.get("recommended_stocks", [])
                stock_tags = ""
                for s in stocks:
                    code = s.get("code", "")
                    sname = s.get("name", "")
                    market = s.get("market", "")
                    stock_tags += (
                        f'<span style="display:inline-block;background:#e6f7ff;'
                        f'border:1px solid #91d5ff;border-radius:4px;padding:4px 10px;'
                        f'margin:3px 6px 3px 0;font-size:13px;">'
                        f'<strong style="color:#0050b3;">{code}</strong> '
                        f'<span style="color:#555;">{sname}</span> '
                        f'<span style="color:#999;font-size:11px;">({market})</span></span>'
                    )
                sector_items += f"""
                <div style="margin:10px 0;padding:12px 16px;background:#fafafa;border-radius:8px;">
                  <div style="font-weight:600;color:#333;font-size:14px;margin-bottom:4px;">
                    📌 {name}
                  </div>
                  <div style="color:#888;font-size:12px;margin-bottom:8px;">{reason}</div>
                  <div>{stock_tags}</div>
                </div>"""

            sector_block = f"""
          <div style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
            <h3 style="margin:0 0 12px;color:#333;font-size:16px;">📊 板块分析与推荐标的</h3>
            {sector_items}
          </div>"""

        # ── 个股提及区块 ──
        stock_block = ""
        if stocks_mentioned:
            stock_items = ""
            for s in stocks_mentioned:
                code = s.get("code", "")
                name = s.get("name", "")
                market = s.get("market", "")
                stock_items += (
                    f'<li style="margin:6px 0;">'
                    f'<strong style="color:#d4380d;">{code}</strong> '
                    f'{name} <span style="color:#888;">({market})</span></li>\n'
                )
            stock_block = f"""
          <div style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
            <h3 style="margin:0 0 12px;color:#333;font-size:16px;">🎯 视频中提及的个股</h3>
            <ul style="padding-left:20px;margin:0;">
              {stock_items}
            </ul>
          </div>"""

        # ── 封面图片 ──
        cover_block = ""
        if cover_url:
            cover_block = f"""
            <div style="text-align:center;margin-bottom:16px;">
              <img src="{cover_url}" style="max-width:100%;border-radius:8px;"
                   alt="视频封面" />
            </div>"""

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.08);overflow:hidden;">

  <!-- 头部 -->
  <div style="background:linear-gradient(135deg,#000000,#333333);padding:20px 24px;color:#fff;">
    <h2 style="margin:0;font-size:20px;">🎵 {video_author} 最新视频要点</h2>
    <p style="margin:6px 0 0;opacity:0.7;font-size:13px;">
      生成于 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    </p>
  </div>

  <!-- 原文（最上方） -->
  {original_block}

  <!-- 个股提及 -->
  {stock_block}

  <!-- 板块推荐 -->
  {sector_block}

  <!-- AI 要点概括 -->
  <div style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
    <h3 style="margin:0 0 10px;color:#333;font-size:16px;">📝 AI 要点概括</h3>
    <blockquote style="margin:0;padding:14px 18px;background:#fafafa;border-left:4px solid #000;border-radius:4px;line-height:1.8;color:#555;white-space:pre-wrap;word-break:break-word;">
{safe_summary}
    </blockquote>
  </div>

  <!-- 视频链接 -->
  <div style="padding:20px 24px;">
    {cover_block}
    <p style="margin:8px 0;color:#333;font-size:14px;">
      <strong>标题：</strong>{safe_title}
    </p>
    <p style="margin-top:14px;">
      <a href="{video_url}" style="display:inline-block;background:#fe2c55;color:#fff;text-decoration:none;padding:10px 24px;border-radius:20px;font-size:14px;" target="_blank">
        ▶ 在抖音中打开视频
      </a>
    </p>
    <p style="margin-top:10px;font-size:12px;color:#999;">
      视频链接：<a href="{video_url}" style="color:#999;">{video_url}</a>
    </p>
  </div>

  <!-- 底部 -->
  <div style="padding:14px 24px;background:#fafafa;border-top:1px solid #f0f0f0;text-align:center;font-size:12px;color:#999;">
    模型先生视频提醒机器人 | AI 生成内容仅供参考，不构成投资建议
  </div>
</div>
</body>
</html>"""
        return html


def load_recipients(filepath: str) -> List[str]:
    """从 CSV 文件加载收件人邮箱（去重 + 过滤过期）。

    CSV 格式：email,expire_date
    - email: 邮箱地址
    - expire_date: 失效日期 (YYYY-MM-DD)，只发送给当前日期 < 失效日期的收件人

    Args:
        filepath: CSV 文件路径

    Returns:
        List[str]: 去重后的有效邮箱列表
    """
    recipients = []
    if not os.path.exists(filepath):
        logger.error(f"收件人文件不存在: {filepath}")
        return recipients

    today = date.today()
    try:
        # utf-8-sig 兼容 Windows Excel 的 BOM 头
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or not row[0].strip():
                    continue
                email = row[0].strip()
                if "@" not in email:
                    continue

                expire_str = (row[1].strip() if len(row) > 1 else "").strip()
                if expire_str:
                    try:
                        expire_date = datetime.strptime(
                            expire_str, "%Y-%m-%d"
                        ).date()
                        if expire_date < today:
                            logger.debug(f"收件人已过期: {email} ({expire_str})")
                            continue
                    except ValueError:
                        logger.warning(
                            f"无法解析失效日期 '{expire_str}'，跳过: {email}"
                        )
                        continue

                recipients.append(email)

    except Exception as e:
        logger.error(f"读取收件人文件失败: {e}")
        return []

    # 去重（大小写不敏感）
    seen = set()
    unique = []
    for r in recipients:
        r_lower = r.lower()
        if r_lower not in seen:
            seen.add(r_lower)
            unique.append(r)

    logger.info(f"成功加载有效收件人 {len(unique)} 个")
    return unique
