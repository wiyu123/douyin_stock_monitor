"""
抖音视频抓取模块
使用 Playwright 自动化浏览器，直接通过抖音号精准定位用户并获取最新视频。
支持字幕提取（优先）和语音转文字（whisper 兜底）。
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from typing import Optional

import httpx
from playwright.async_api import async_playwright, Browser, Page

logger = logging.getLogger(__name__)

# ── 反检测 User-Agent ──
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# 视频 ID 正则
VIDEO_ID_RE = re.compile(r"/video/(\d+)")


class DouyinScraper:
    """抖音视频抓取器 (基于 Playwright 浏览器自动化)

    通过抖音号直接定位用户主页，提取最新视频 + 字幕/语音转文字。
    """

    def __init__(
        self,
        douyin_id: str = "moxingxiansheng",
        headless: bool = True,
        timeout: int = 30000,
        enable_whisper: bool = False,
    ):
        self.douyin_id = douyin_id
        self.headless = headless
        self.timeout = timeout
        self.enable_whisper = enable_whisper
        self.user_url = f"https://www.douyin.com/user/{douyin_id}"
        self._playwright = None
        self._browser: Optional[Browser] = None

    # ── 浏览器管理 ─────────────────────────────────────────

    async def _ensure_browser(self) -> Browser:
        """确保浏览器已启动"""
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            logger.info("Playwright 浏览器已启动")
        return self._browser

    async def _new_page(self) -> Page:
        """创建带反检测配置的新页面"""
        browser = await self._ensure_browser()
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        # 反检测脚本
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) => (
                p.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                origQuery(p)
            );
        """)
        page = await context.new_page()
        page.set_default_timeout(self.timeout)
        return page

    # ── 主抓取方法 ─────────────────────────────────────────

    async def get_latest_video(self) -> Optional[dict]:
        """获取目标用户的最新视频信息（含字幕/语音转文字原文）

        Returns:
            dict: {
                "video_id": str,
                "title": str,
                "description": str,       # 视频作者写的文案
                "url": str,
                "publish_time": datetime | None,
                "cover_url": str,
                "author_name": str,
                "original_text": str,      # 原文：字幕文本或语音转文字结果
                "text_source": str,        # "subtitle" | "whisper" | "none"
            }
            失败返回 None
        """
        page = await self._new_page()
        try:
            # ── 1. 直接访问用户主页 ──
            logger.info(f"正在访问用户主页: {self.user_url}")
            await page.goto(self.user_url, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            # 检查是否重定向到了 sec_uid 格式的 URL
            current_url = page.url
            logger.info(f"当前页面 URL: {current_url}")

            # ── 2. 从主页提取最新视频 ──
            video_basic = await page.evaluate("""
                () => {
                    // 查找所有视频链接
                    const videoLinks = document.querySelectorAll('a[href*="/video/"]');
                    if (videoLinks.length === 0) return null;

                    const firstLink = videoLinks[0];
                    const href = firstLink.getAttribute('href');
                    const match = href && href.match(/\\/video\\/(\\d+)/);
                    const videoId = match ? match[1] : '';

                    let description = '', title = '';
                    const card = firstLink.closest('[class*="item"], [class*="card"], [class*="video"], li, div');

                    if (card) {
                        const descEl = card.querySelector('[class*="desc"], [class*="title"], [class*="content"]');
                        if (descEl) {
                            description = (descEl.textContent || '').trim();
                            title = description.substring(0, 100);
                        }
                        if (!description) {
                            const allText = (card.textContent || '').trim();
                            const lines = allText.split('\\n').map(s => s.trim()).filter(s => s.length > 5);
                            description = lines.join(' ');
                            title = lines[0] || '';
                        }
                    }

                    if (!description) {
                        description = (firstLink.textContent || firstLink.getAttribute('aria-label') || '').trim();
                        title = description.substring(0, 100);
                    }

                    // 封面图
                    let coverUrl = '';
                    const imgEl = (card || firstLink).querySelector('img');
                    if (imgEl) {
                        coverUrl = imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '';
                    }

                    return { videoId, title, description, coverUrl, href };
                }
            """)

            if not video_basic or not video_basic.get("videoId"):
                logger.warning("未能从用户主页提取到视频信息")
                return None

            video_id = video_basic["videoId"]
            video_url = f"https://www.douyin.com/video/{video_id}"
            logger.info(f"最新视频 ID: {video_id}")

            # ── 3. 进入视频详情页，提取完整描述 + 字幕 ──
            description = video_basic.get("description", "")
            title = video_basic.get("title", "")
            cover_url = video_basic.get("coverUrl", "")
            original_text = ""
            text_source = "none"
            author_name = self.douyin_id

            try:
                logger.info(f"正在访问视频详情页: {video_url}")
                await page.goto(video_url, wait_until="domcontentloaded")
                await asyncio.sleep(3)

                detail = await page.evaluate("""
                    () => {
                        let desc = '', authorName = '', subtitleText = '';

                        // ── 提取视频描述 ──
                        const descSelectors = [
                            '[class*="video-info-desc"]',
                            '[data-e2e="video-desc"]',
                            '#video-info-desc',
                        ];
                        for (const sel of descSelectors) {
                            try {
                                const el = document.querySelector(sel);
                                if (el && el.textContent && el.textContent.trim().length > 10) {
                                    desc = el.textContent.trim();
                                    break;
                                }
                            } catch(e) {}
                        }
                        if (!desc) {
                            const metaDesc = document.querySelector('meta[name="description"]');
                            if (metaDesc) desc = metaDesc.getAttribute('content') || '';
                        }

                        // ── 提取作者名 ──
                        const authorSelectors = [
                            '[class*="author-name"]',
                            '[class*="nickname"]',
                            '[data-e2e="user-info-nickname"]',
                        ];
                        for (const sel of authorSelectors) {
                            try {
                                const el = document.querySelector(sel);
                                if (el && el.textContent && el.textContent.trim()) {
                                    authorName = el.textContent.trim();
                                    break;
                                }
                            } catch(e) {}
                        }

                        // ── 提取字幕/原文：从页面嵌入式数据中查找 ──
                        // 策略1：查找包含 subtitle/caption 的 script 或 JSON 数据
                        try {
                            const scripts = document.querySelectorAll('script');
                            for (const script of scripts) {
                                const text = script.textContent || '';
                                // 尝试查找 __UNIVERSAL_DATA__ 或 RENDER_DATA
                                if (text.includes('subtitle') || text.includes('caption')) {
                                    // 提取字幕文本
                                    const subtitleMatches = text.match(/"content":"([^"]+)"/g);
                                    if (subtitleMatches) {
                                        const contents = subtitleMatches.map(m => {
                                            try {
                                                return JSON.parse('{' + m + '}').content;
                                            } catch { return ''; }
                                        }).filter(Boolean);
                                        if (contents.length > 0) {
                                            subtitleText = contents.join('');
                                        }
                                    }
                                }
                            }
                        } catch(e) {}

                        // 策略2：查找页面中可见的字幕元素
                        if (!subtitleText) {
                            const subtitleEls = document.querySelectorAll(
                                '[class*="subtitle"], [class*="caption"], [class*="video-subtitle"]'
                            );
                            const texts = [];
                            subtitleEls.forEach(el => {
                                if (el.textContent && el.textContent.trim()) {
                                    texts.push(el.textContent.trim());
                                }
                            });
                            if (texts.length > 0) subtitleText = texts.join('\\n');
                        }

                        // ── 从页面所有内嵌 JSON 中提取更多字幕数据 ──
                        if (!subtitleText) {
                            try {
                                // 尝试从 #RENDER_DATA 或 __UNIVERSAL_DATA__ 提取
                                const allText = document.documentElement.innerHTML;
                                // 查找 subtitle_content_list 或类似的字幕数据
                                const subListMatch = allText.match(/"subtitle_content_list"\\s*:\\s*(\\[[^\\]]*\\])/);
                                if (subListMatch) {
                                    try {
                                        const list = JSON.parse(subListMatch[1]);
                                        subtitleText = list.map(item => item.content || item.text || '').join('');
                                    } catch {}
                                }
                                // 查找 content 数组（字幕通常是一系列带时间戳和内容的条目）
                                if (!subtitleText) {
                                    const contentListMatch = allText.match(/"content"\\s*:\\s*"([^"]+)"/g);
                                    if (contentListMatch) {
                                        const texts = contentListMatch.map(m => {
                                            const v = m.match(/"content"\\s*:\\s*"([^"]+)"/);
                                            return v ? v[1] : '';
                                        }).filter(s => s.length > 1 && !s.startsWith('http'));
                                        if (texts.length > 3) {
                                            subtitleText = texts.join('');
                                        }
                                    }
                                }
                            } catch(e) {}
                        }

                        return {
                            description: desc,
                            authorName: authorName,
                            subtitleText: subtitleText,
                            pageTitle: document.title || '',
                        };
                    }
                """)

                if detail:
                    if detail.get("description") and len(detail["description"]) > len(description):
                        description = detail["description"]
                        if not title:
                            title = description[:100]

                    if detail.get("authorName"):
                        author_name = detail["authorName"]

                    if detail.get("subtitleText"):
                        original_text = detail["subtitleText"]
                        text_source = "subtitle"
                        logger.info(f"从页面提取到字幕，长度: {len(original_text)}")

            except Exception as e:
                logger.warning(f"获取视频详情页失败: {e}")

            # ── 4. 如果没有字幕，尝试通过 API 获取 ──
            if not original_text:
                logger.info("页面无字幕数据，尝试通过接口获取...")
                subtitle_text = await self._fetch_subtitles_api(video_id)
                if subtitle_text:
                    original_text = subtitle_text
                    text_source = "subtitle"

            # ── 5. 语音转文字兜底（需配置 enable_whisper: true）──
            if not original_text and self.enable_whisper:
                logger.info("无字幕可用，尝试语音转文字（whisper）...")
                transcript = await self._transcribe_video_audio(video_id)
                if transcript:
                    original_text = transcript
                    text_source = "whisper"
                    logger.info(f"语音转文字完成，长度: {len(original_text)}")
                else:
                    logger.warning("语音转文字失败")
            elif not original_text:
                logger.info("未启用 whisper，跳过语音转文字")

            # ── 6. 构建返回结果 ──
            video_info = {
                "video_id": video_id,
                "title": title or (description[:100] if description else ""),
                "description": description,
                "url": video_url,
                "publish_time": None,
                "cover_url": cover_url,
                "author_name": author_name,
                "original_text": original_text,
                "text_source": text_source,
            }

            logger.info(
                f"视频抓取完成: ID={video_id}, "
                f"描述长度={len(description)}, "
                f"原文长度={len(original_text)} "
                f"({text_source})"
            )
            return video_info

        except Exception as e:
            logger.error(f"获取最新视频异常: {e}", exc_info=True)
            return None
        finally:
            await page.context.close()

    # ── 字幕 API 提取 ─────────────────────────────────────

    async def _fetch_subtitles_api(self, video_id: str) -> Optional[str]:
        """尝试通过 Douyin 内部 API 获取字幕数据"""
        page = await self._new_page()
        try:
            video_url = f"https://www.douyin.com/video/{video_id}"
            await page.goto(video_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # 拦截可能包含字幕的网络请求
            subtitle_text = await page.evaluate("""
                () => {
                    // 从整个 HTML 中查找可能的字幕数据
                    const html = document.documentElement.innerHTML;

                    // 尝试匹配 "subtitle_content" 或 "subtitle_text" 相关的 JSON
                    const patterns = [
                        /"subtitle_content_list"\\s*:\\s*(\\[[\\s\\S]*?\\])\\s*[,}]/,
                        /"captions"\\s*:\\s*(\\[[\\s\\S]*?\\])\\s*[,}]/,
                        /"subtitle_text"\\s*:\\s*"([\\s\\S]*?)"/,
                        /"content"\\s*:\\s*"([^"]+)"\\s*[,}]/g,
                    ];

                    for (const pattern of patterns) {
                        let match;
                        while ((match = pattern.exec(html)) !== null) {
                            try {
                                if (pattern.toString().includes('subtitle_content_list') ||
                                    pattern.toString().includes('captions')) {
                                    const list = JSON.parse(match[1]);
                                    const texts = list.map(item =>
                                        item.content || item.text || item.subtitle_content || ''
                                    ).filter(Boolean);
                                    if (texts.length > 0) return texts.join('');
                                } else if (pattern.toString().includes('subtitle_text')) {
                                    if (match[1] && match[1].length > 10) return match[1];
                                }
                            } catch(e) {}
                        }
                    }
                    return '';
                }
            """)

            return subtitle_text if subtitle_text else None

        except Exception as e:
            logger.debug(f"API 字幕提取失败: {e}")
            return None
        finally:
            await page.context.close()

    # ── 语音转文字（whisper 兜底）─────────────────────────

    async def _transcribe_video_audio(self, video_id: str) -> Optional[str]:
        """下载视频音频并用 whisper 转文字

        先尝试从页面提取视频直链，用 httpx 下载音频片段后用 whisper 转文字。
        需要安装: pip install openai-whisper
        需要 ffmpeg 在 PATH 中。
        """
        video_url = f"https://www.douyin.com/video/{video_id}"

        # Step 1: 从视频页面提取无水印视频直链
        page = await self._new_page()
        audio_url = None
        try:
            await page.goto(video_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # 从页面数据中提取视频 URL
            audio_url = await page.evaluate("""
                () => {
                    const html = document.documentElement.innerHTML;

                    // 匹配视频播放地址（无水印）
                    const patterns = [
                        /"play_addr"\\s*:\\s*\\{[^}]*"url_list"\\s*:\\s*\\["([^"]+)"\\]/,
                        /"play_addr_h264"\\s*:\\s*\\{[^}]*"url_list"\\s*:\\s*\\["([^"]+)"\\]/,
                        /"download_addr"\\s*:\\s*\\{[^}]*"url_list"\\s*:\\s*\\["([^"]+)"\\]/,
                        /"bit_rate"\\s*:.*?"play_addr"\\s*:\\s*\\{[^}]*"url_list"\\s*:\\s*\\["([^"]+)"\\]/,
                    ];

                    for (const p of patterns) {
                        const m = html.match(p);
                        if (m && m[1]) return m[1].replace(/\\\\u0026/g, '&');
                    }

                    // 后备：直接匹配视频文件 URL
                    const videoMatch = html.match(/"url_list"\\s*:\\s*\\["([^"]*\\.mp4[^"]*)"\\]/);
                    if (videoMatch) return videoMatch[1].replace(/\\\\u0026/g, '&');

                    return '';
                }
            """)

            if audio_url:
                logger.info(f"获取到视频直链: {audio_url[:80]}...")
            else:
                logger.warning("未能提取视频直链")
                return None

        except Exception as e:
            logger.error(f"提取视频 URL 失败: {e}")
            return None
        finally:
            await page.context.close()

        # Step 2: 下载音频并用 whisper 转文字
        try:
            import whisper
        except ImportError:
            logger.warning(
                "未安装 openai-whisper。安装命令: pip install openai-whisper\n"
                "还需要安装 ffmpeg: https://ffmpeg.org/download.html"
            )
            return None

        # 下载视频到临时文件
        tmp_video = os.path.join(tempfile.gettempdir(), f"douyin_{video_id}.mp4")
        try:
            logger.info(f"正在下载视频音频...")
            async with httpx.AsyncClient(
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": "https://www.douyin.com/",
                },
                timeout=120,
                follow_redirects=True,
            ) as client:
                resp = await client.get(audio_url)
                if resp.status_code == 200:
                    with open(tmp_video, "wb") as f:
                        f.write(resp.content)
                    file_size = len(resp.content)
                    logger.info(f"视频下载完成: {file_size / 1024 / 1024:.1f} MB")
                else:
                    logger.error(f"视频下载失败: HTTP {resp.status_code}")
                    return None

            # whisper 转文字
            logger.info("正在语音转文字 (whisper base model)...")

            def _transcribe_sync():
                model = whisper.load_model("base")
                result = model.transcribe(
                    tmp_video,
                    language="zh",
                    verbose=False,
                )
                return result.get("text", "").strip()

            # 在 executor 中运行（whisper 是同步的）
            text = await asyncio.get_event_loop().run_in_executor(
                None, _transcribe_sync
            )
            return text

        except Exception as e:
            logger.error(f"语音转文字失败: {e}", exc_info=True)
            return None
        finally:
            # 清理临时文件
            try:
                if os.path.exists(tmp_video):
                    os.remove(tmp_video)
                    logger.debug(f"已清理临时文件: {tmp_video}")
            except Exception:
                pass

    async def close(self):
        """关闭浏览器和 Playwright"""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        logger.info("Playwright 浏览器已关闭")


# ── 自测 ──
async def _test():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    scraper = DouyinScraper(douyin_id="moxingxiansheng", headless=False)
    try:
        video = await scraper.get_latest_video()
        if video:
            print(f"\n{'='*50}")
            print(f"视频ID: {video['video_id']}")
            print(f"作者: {video['author_name']}")
            print(f"标题: {video['title'][:80]}")
            print(f"链接: {video['url']}")
            print(f"原文来源: {video['text_source']}")
            print(f"原文内容 ({len(video['original_text'])} 字):")
            print(video['original_text'][:500])
            print(f"{'='*50}")
        else:
            print("未获取到视频")
    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(_test())
