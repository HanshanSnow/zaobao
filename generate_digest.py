import json
import os
import re
import sys
from datetime import datetime
from html import unescape

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def html_to_text(html, max_len=800):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True)
    text = unescape(text)
    text = re.sub(r"[\s]+", " ", text)
    text = re.sub(r"\n+", "\n", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def fetch_rss(source):
    """抓取 RSS 源的文章"""
    items = []
    try:
        feed = feedparser.parse(source["url"])
        limit = source.get("limit", 10)
        for entry in feed.entries[:limit]:
            # 优先取正文，否则取摘要
            content = ""
            if "content" in entry:
                content = entry.content[0].value
            elif "summary" in entry:
                content = entry.summary
            elif "description" in entry:
                content = entry.description

            items.append({
                "title": entry.get("title", "未命名").strip(),
                "url": entry.get("link", "").strip(),
                "source": source.get("name", "未知来源"),
                "body": html_to_text(content, max_len=600),
            })
    except Exception as e:
        print(f"抓取 {source.get('name')} 失败: {e}", file=sys.stderr)
    return items


def call_ai(articles, interests, config):
    """调用 AI 一次性汇总所有文章"""
    api_base = config["ai"]["api_base"]
    model = config["ai"]["model"]
    api_key = os.environ.get("AI_API_KEY", "")

    if not api_key:
        raise RuntimeError("环境变量 AI_API_KEY 未设置")

    # 构建文章列表文本
    article_texts = []
    for i, a in enumerate(articles, 1):
        article_texts.append(
            f"{i}. 标题：{a['title']}\n   来源：{a['source']}\n   链接：{a['url']}\n   正文节选：{a['body']}"
        )

    interests_str = ", ".join(interests) if interests else "（未设置）"

    prompt = (
        "你是我的早报编辑。下面是从多个 RSS 源获取的文章。请为每篇文章做三件事：\n"
        "1. 筛选：删掉明显无关、低质量或重复的文章；\n"
        "2. 摘要：为保留的每篇写 80~120 字中文摘要；\n"
        "3. 标注：与用户兴趣直接相关的 tag 写 \"重点\"，比较相关写 \"关注\"，其余写 \"推荐\"；"
        "同时在 reason 字段写清楚与哪个兴趣相关。\n\n"
        "只输出一个 JSON 数组，不要任何额外文字、不要代码块标记。\n"
        "数组元素格式：{\"title\":\"原文标题\",\"source\":\"来源名\",\"url\":\"链接\",\"summary\":\"摘要\",\"tag\":\"重点|关注|推荐\",\"reason\":\"原因\"}\n\n"
        f"用户兴趣：{interests_str}\n\n"
        f"文章列表：\n{chr(10).join(article_texts)}"
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = requests.post(api_base, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]

    # 清理 AI 可能包裹的代码块
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.S).strip()

    # 提取 JSON 数组部分
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]

    return json.loads(cleaned)


def fallback_articles(articles, interests):
    """如果 AI 调用失败，直接返回原始文章，不做 AI 摘要"""
    result = []
    for a in articles:
        full_text = a["title"] + " " + a["body"]
        matched = [kw for kw in interests if kw in full_text]
        if matched:
            tag = "重点"
            reason = f"命中兴趣：{', '.join(matched)}"
        else:
            tag = "推荐"
            reason = ""
        result.append({
            "title": a["title"],
            "source": a["source"],
            "url": a["url"],
            "summary": a["body"][:160] + "…" if len(a["body"]) > 160 else a["body"],
            "tag": tag,
            "reason": reason,
        })
    return result


def main():
    config = load_config()
    interests = config.get("interests", [])
    sources = config.get("sources", [])

    # 1. 抓取所有文章
    all_articles = []
    for source in sources:
        if source.get("type") == "rss":
            all_articles.extend(fetch_rss(source))

    if not all_articles:
        print("没有抓到任何文章", file=sys.stderr)
        all_articles = []

    # 2. 调用 AI 汇总
    try:
        items = call_ai(all_articles, interests, config)
    except Exception as e:
        print(f"AI 汇总失败，使用兜底方案：{e}", file=sys.stderr)
        items = fallback_articles(all_articles, interests)

    # 3. 生成 digest.json
    today = datetime.now().strftime("%Y-%m-%d")
    digest = {
        "digest": f"早上好 ☀️ 今天是 {today}。以下是为你汇总的早报内容。",
        "items": items,
    }

    with open("digest.json", "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    print(f"已生成 digest.json，共 {len(items)} 条")


if __name__ == "__main__":
    main()
