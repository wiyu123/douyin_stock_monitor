"""
大语言模型视频概括模块
使用 DeepSeek API（OpenAI 兼容格式）概括抖音视频内容，识别板块和个股。
"""

import json
import logging
import re
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# ── System Prompt ──
SYSTEM_PROMPT = """你是一位专业的中国股市分析师。用户会提供一段抖音财经博主的视频内容文本，请你：

1. **要点概括**：用 2-4 句话概括视频的核心观点和内容。

2. **板块识别与推荐**：如果视频内容中提到了具体的行业/板块（如"新能源汽车"、"半导体"、"白酒"、"光伏"、"人工智能"、"医药"、"银行"等），请列出这些板块，并针对每个板块推荐 2-3 个 A 股核心标的（含股票代码和名称）。推荐的标的应该是该板块的龙头或代表性公司。

3. **个股提取**：如果视频中明确提到了具体的股票名称或代码，请完整列出。

**重要规则**：
- 只根据视频内容进行分析，不要编造未提及的信息
- 如果视频没有提到任何板块或个股，对应字段返回空数组
- 推荐的板块标的必须是真实存在的 A 股，代码为 6 位数字
- 用中文输出
- 严格按照 JSON 格式输出，不要输出任何 JSON 之外的内容

输出 JSON 格式：
{
  "summary": "视频核心要点概括文字...",
  "sectors": [
    {
      "name": "板块名称",
      "reason": "视频中如何提及该板块",
      "recommended_stocks": [
        {"code": "600xxx", "name": "股票名称", "market": "A股上海主板"},
        {"code": "300xxx", "name": "股票名称", "market": "A股创业板"}
      ]
    }
  ],
  "stocks_mentioned": [
    {"code": "600519", "name": "贵州茅台", "market": "A股上海主板"}
  ]
}"""


class LLMSummarizer:
    """视频内容 LLM 概括器（DeepSeek API）"""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        max_tokens: int = 2048,
    ):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    def summarize(
        self, title: str = "", description: str = "", original_text: str = ""
    ) -> dict:
        """对视频内容进行概括和标的识别

        Args:
            title: 视频标题
            description: 视频描述/文案（作者写的）
            original_text: 视频原文/字幕/语音转文字内容（博主说的话）

        Returns:
            dict: {
                "summary": str,
                "sectors": list[dict],
                "stocks_mentioned": list[dict],
                "raw_json": str,
            }
        """
        # 构建用户消息 — 原文是最重要的分析素材
        parts = ["以下是抖音财经博主最新视频的内容，请分析："]
        if original_text:
            parts.append(f"\n【视频原文（博主口播内容，最重要）】\n{original_text}")
        if description:
            parts.append(f"\n【视频文案/简介】\n{description}")
        if title:
            parts.append(f"\n【视频标题】\n{title}")

        user_content = "\n".join(parts)

        logger.info(f"正在调用 DeepSeek API (model={self.model})...")

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )

            raw_text = response.choices[0].message.content if response.choices else ""
            logger.info(f"DeepSeek 返回 {len(raw_text)} 字符")

            # 解析 JSON
            parsed = self._parse_json(raw_text)
            parsed["raw_json"] = raw_text
            return parsed

        except Exception as e:
            logger.error(f"DeepSeek API 调用失败: {e}", exc_info=True)
            return {
                "summary": f"（API 调用失败: {e}）",
                "sectors": [],
                "stocks_mentioned": [],
                "raw_json": "",
            }

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从 LLM 返回文本中提取 JSON"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试从 ```json ... ``` 代码块中提取
        json_block_match = re.search(
            r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text
        )
        if json_block_match:
            try:
                return json.loads(json_block_match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试从 { ... } 中提取（找最外层大括号）
        brace_match = re.search(r'\{[\s\S]*\}', text)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        # 解析失败，返回原始文本作为 summary
        logger.warning("无法解析 LLM 返回的 JSON，将原始文本作为 summary")
        return {
            "summary": text[:500],
            "sectors": [],
            "stocks_mentioned": [],
        }


# ── 自测 ──
def _test():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    import os
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("请设置 DEEPSEEK_API_KEY 环境变量后运行测试")
        return

    summarizer = LLMSummarizer(api_key=api_key)
    result = summarizer.summarize(
        title="半导体板块重大利好，国家大基金三期来了",
        description=(
            "今天跟大家聊聊半导体板块的最新动态。国家大基金三期正式成立，"
            "注册资本超3000亿，这意味着什么？重点关注中芯国际、北方华创这些龙头。"
            "另外新能源汽车这边，比亚迪的销量又创新高，整个产业链都受益。"
        ),
    )
    print(f"\n要点概括: {result['summary']}")
    print(f"板块推荐: {json.dumps(result['sectors'], ensure_ascii=False, indent=2)}")
    print(f"个股提及: {json.dumps(result['stocks_mentioned'], ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    _test()
