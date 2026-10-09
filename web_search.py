"""
联网搜题: 用 DuckDuckGo HTML (无 API key) + WebFetch 抓页面摘要
- search(query) -> 候选标题+链接列表
- fetch_summary(url) -> 页面文字摘要
- solve_with_search(question, subject) -> 整合后的"资料包"
"""
import re
import requests
from urllib.parse import unquote, urlparse
from typing import List, Dict

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TIMEOUT = 15


def _ddg_html_search(query: str, max_results: int = 5) -> List[Dict]:
    """
    DuckDuckGo HTML 接口, 免 API key
    """
    url = "https://html.duckduckgo.com/html/"
    try:
        r = requests.post(url, data={'q': query}, headers={'User-Agent': UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        return [{'title': '搜索失败', 'href': '', 'snippet': str(e), 'error': True}]

    html = r.text
    results = []

    # 解析 result block: 简单正则即可
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.S
    )
    for href, title_html, snip_html in blocks[:max_results]:
        title = re.sub(r'<[^>]+>', '', title_html).strip()
        snippet = re.sub(r'<[^>]+>', '', snip_html).strip()
        # DDG 跳转链接 -> 解码 uddg
        if 'uddg=' in href:
            m = re.search(r'uddg=([^&]+)', href)
            if m:
                from urllib.parse import unquote
                href = unquote(m.group(1))
        results.append({
            'title': title,
            'href': href,
            'snippet': snippet,
            'error': False,
        })
    if not results:
        # DDG 偶尔回退, 给个提示
        results.append({
            'title': '未匹配到结果', 'href': '',
            'snippet': '可能 DDG 临时限流, 稍后重试或换关键词.',
            'error': False,
        })
    return results


def _bing_html_search(query: str, max_results: int = 5) -> List[Dict]:
    """
    Bing 国内版 fallback
    """
    url = "https://cn.bing.com/search"
    try:
        r = requests.get(url, params={'q': query}, headers={'User-Agent': UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        return [{'title': 'Bing 搜索失败', 'href': '', 'snippet': str(e), 'error': True}]
    html = r.text
    results = []
    # 必应国内版结构
    blocks = re.findall(
        r'<li[^>]+class="b_algo"[^>]*>.*?<h2>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?</h2>.*?'
        r'<p[^>]*>(.*?)</p>',
        html, re.S
    )
    for href, title_html, snip_html in blocks[:max_results]:
        title = re.sub(r'<[^>]+>', '', title_html).strip()
        snippet = re.sub(r'<[^>]+>', '', snip_html).strip()
        if title and href.startswith('http'):
            results.append({'title': title, 'href': href, 'snippet': snippet, 'error': False})
    if not results:
        results.append({'title': 'Bing 未匹配到结果', 'href': '',
                        'snippet': '可能限流, 稍后重试.', 'error': False})
    return results


def _multi_engine_search(query: str, max_results: int = 5) -> List[Dict]:
    """多引擎兜底: DDG -> Bing"""
    res = _ddg_html_search(query, max_results)
    # 过滤掉错误和"未匹配"占位
    real = [r for r in res if not r.get('error') and '未匹配' not in r.get('title', '')]
    if real:
        return real
    return _bing_html_search(query, max_results)


def fetch_page_text(url: str, max_chars: int = 2000) -> str:
    """简单抓页面, 去标签, 取正文前 max_chars 字符"""
    if not url or not url.startswith('http'):
        return ''
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        text = re.sub(r'<script[^>]*>.*?</script>', '', r.text, flags=re.S | re.I)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.S | re.I)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[抓取失败: {type(e).__name__}: {e}]"


def solve_with_search(question: str, subject: str = '通用',
                      fetch_top: int = 2, max_chars: int = 1500) -> dict:
    """
    主入口: 搜题 + 抓前 2 个链接的正文摘要
    """
    if not question or not question.strip():
        return {'results': [], 'summary': '题目为空', 'source': 'empty'}

    # 学科前缀增强命中率
    query = f"{subject} {question[:80]}" if subject and subject != '通用' else question[:80]
    results = _multi_engine_search(query, max_results=fetch_top + 2)

    summary_parts = []
    fetched = 0
    for r in results:
        if r.get('error') or not r.get('href'):
            continue
        text = fetch_page_text(r['href'], max_chars=max_chars // max(fetch_top, 1))
        if text and not text.startswith('['):
            summary_parts.append(f"【{r['title']}】\n{text}")
            fetched += 1
            if fetched >= fetch_top:
                break

    summary = '\n\n---\n\n'.join(summary_parts) if summary_parts else '未抓取到正文.'
    return {
        'query': query,
        'results': results,
        'summary': summary,
        'source': 'duckduckgo+bing',
    }
