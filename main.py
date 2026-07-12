#!/usr/bin/env python3
"""
抖音博主"模型先生"视频监控与股票推荐邮件系统
抓取最新视频 → LLM 概括要点 → 识别板块/个股 → 邮件推送
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

import yaml

from douyin_scraper import DouyinScraper
from llm_summarizer import LLMSummarizer
from tracker import VideoTracker
from mailer import Mailer, load_recipients

# ── 日志配置 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("douyin_monitor")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")


def load_config() -> dict:
    """加载 YAML 配置"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        logger.info("配置加载成功")
        return cfg
    except FileNotFoundError:
        logger.error(f"配置文件不存在: {CONFIG_FILE}")
        logger.error("请复制 config.yaml.example 为 config.yaml 并填写配置")
        sys.exit(1)
    except yaml.YAMLError as e:
        logger.error(f"配置文件解析失败: {e}")
        sys.exit(1)


def validate_config(cfg: dict) -> bool:
    """验证配置完整性，返回是否通过"""
    errors = []

    llm_cfg = cfg.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    if not api_key or api_key == "YOUR_ANTHROPIC_API_KEY_HERE":
        errors.append("请在 config.yaml 中填写 llm.api_key (Anthropic API Key)")

    smtp_cfg = cfg.get("smtp", {})
    if not smtp_cfg.get("username") or smtp_cfg.get("username") == "YOUR_SMTP_USERNAME":
        errors.append("请在 config.yaml 中填写 smtp.username (阿里云发信地址)")
    if not smtp_cfg.get("password") or smtp_cfg.get("password") == "YOUR_SMTP_PASSWORD":
        errors.append("请在 config.yaml 中填写 smtp.password (SMTP 密码)")

    if errors:
        for e in errors:
            logger.error(f"配置错误: {e}")
        return False
    return True


async def main():
    """主流程"""
    logger.info("=" * 50)
    logger.info("\U0001F3B5 抖音博主 模型先生 视频监控启动")
    logger.info("=" * 50)

    # 1. 加载配置
    cfg = load_config()
    if not validate_config(cfg):
        sys.exit(1)

    # 2. 加载收件人列表
    email_file = cfg.get("email_list_file", "emails.csv")
    recipients = load_recipients(os.path.join(BASE_DIR, email_file))
    if not recipients:
        logger.error("没有有效的收件人，请检查 emails.csv")
        sys.exit(1)
    logger.info(f"收件人: {len(recipients)} 个")

    # 3. 初始化各模块
    dy_cfg = cfg.get("douyin", {})
    scraper = DouyinScraper(
        douyin_id=dy_cfg.get("douyin_id", "moxingxiansheng"),
        headless=dy_cfg.get("headless", True),
        timeout=dy_cfg.get("timeout", 30000),
        enable_whisper=dy_cfg.get("enable_whisper", False),
    )

    llm_cfg = cfg.get("llm", {})
    summarizer = LLMSummarizer(
        api_key=llm_cfg.get("api_key", ""),
        model=llm_cfg.get("model", "claude-sonnet-20250601"),
        base_url=llm_cfg.get("base_url", "https://api.anthropic.com"),
        max_tokens=llm_cfg.get("max_tokens", 2048),
    )

    tracker = VideoTracker()

    smtp_cfg = cfg.get("smtp", {})
    mailer = Mailer(smtp_cfg)

    try:
        # 4. 抓取最新视频
        logger.info(f"正在抓取抖音号 @{scraper.douyin_id} 的最新视频...")
        video = await scraper.get_latest_video()

        if not video:
            logger.warning("未能获取到视频，可能网络问题或页面结构变化")
            logger.info("提示：可设置 headless: false 观察浏览器运行情况")
            return

        video_id = video["video_id"]
        logger.info(f"获取到视频: ID={video_id}")
        logger.info(f"  标题: {video['title'][:80]}")
        logger.info(f"  链接: {video['url']}")
        logger.info(f"  原文来源: {video.get('text_source', 'none')} "
                     f"({len(video.get('original_text', ''))} 字)")

        # 5. 去重检查
        if tracker.is_processed(video_id):
            logger.info(f"视频 {video_id} 已处理过，跳过")
            return

        # 6. LLM 概括 + 标的识别（原文是主要分析素材）
        logger.info("正在调用 Claude API 进行视频内容分析...")
        analysis = summarizer.summarize(
            title=video.get("title", ""),
            description=video.get("description", ""),
            original_text=video.get("original_text", ""),
        )

        logger.info(f"要点概括: {analysis['summary'][:120]}...")
        logger.info(
            f"识别到 {len(analysis.get('sectors', []))} 个板块, "
            f"{len(analysis.get('stocks_mentioned', []))} 个具体个股"
        )

        # 7. 发送邮件
        logger.info(f"正在发送邮件给 {len(recipients)} 位收件人...")
        success = mailer.send_video_alert(
            to_addrs=recipients,
            video_info=video,
            analysis=analysis,
        )

        # 8. 标记已处理
        if success:
            tracker.mark_done(video_id, summary=analysis.get("summary", ""))
            logger.info("[OK] 完成！视频要点已成功推送给所有收件人")
        else:
            logger.warning("[WARN] 邮件发送失败，视频未标记为已处理（下次运行将重试）")

    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"运行异常: {e}", exc_info=True)
    finally:
        await scraper.close()
        tracker.close()
        logger.info("再见!")


if __name__ == "__main__":
    asyncio.run(main())
