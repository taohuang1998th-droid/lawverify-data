#!/usr/bin/env python3
"""
court_source.py — 最高人民法院官网司法解释爬虫
===============================================

封装 court.gov.cn 司法解释专栏的完整爬取逻辑，
对外只暴露一个高级方法 crawl()，返回标准 patch 列表。

扩展新数据源时可参照本文件：
  1. 实现 list_page_url(n) — 返回第 n 页的 URL
  2. 实现 _parse_list_page(soup) — 从列表 HTML 提取条目
  3. 实现 fetch_detail_text(session, url) — 从详情页获取纯文本
  4. 实现 extract_law_name(raw_text, fallback_title) — 从正文提取法律名称
  5. crawl() 调用 merge.build_patches() 生成统一格式的 patch

无需修改 merge.py 或 sync_court_laws.py 即可接入新源。
"""

from __future__ import annotations

import datetime
import re
import time
from typing import Iterator

import requests
from bs4 import BeautifulSoup

from merge import build_patches, parse_articles

# ── 站点常量 ──────────────────────────────────────────────────────────────────
_SITE        = "https://www.court.gov.cn"
_LIST_TMPL   = _SITE + "/fabu/gengduo/16{suffix}.html"
_CONTENT_SEL = ".txt"

class SourceUnavailableError(RuntimeError):
    """数据源不可用（网络失败 / 页面结构变更 / 详情页全部失败）。

    与「源正常但时间窗口内无新条文」严格区分：后者返回空列表正常退出，
    前者必须让调用方以非零退出码结束，使 CI 变红报警——否则故障会被
    伪装成"今天没有新法"（NPC 管线曾因此静默失效 23 天）。
    """


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": _SITE,
}


class CourtGovSource:
    """
    最高人民法院官网 (court.gov.cn) 司法解释专栏爬虫。

    典型用法：
        source  = CourtGovSource()
        with requests.Session() as s:
            patches = source.crawl(s, full=True)
            # patches 已是 build_patches() 格式，可直接交给 LawDBMerger
    """

    site        = _SITE
    list_tmpl   = _LIST_TMPL
    content_sel = _CONTENT_SEL
    law_type    = "interpretation"

    def __init__(self, headers: dict | None = None,
                 request_delay: float = 0.8) -> None:
        self.headers       = headers or _DEFAULT_HEADERS
        self.request_delay = request_delay   # 礼貌延迟（秒）

    # ── URL 生成 ───────────────────────────────────────────────────────────────

    def list_page_url(self, page: int) -> str:
        return self.list_tmpl.format(suffix="" if page == 1 else f"_{page}")

    # ── 分页列表解析 ───────────────────────────────────────────────────────────

    def _parse_list_page(self, soup: BeautifulSoup) -> list[dict]:
        """从列表页 HTML 提取条目列表，每项包含 title / url / date。"""
        for ul in soup.find_all("ul"):
            lis = ul.find_all("li")
            hits = [li for li in lis
                    if li.find("a") and "xiangqing" in (li.find("a").get("href") or "")]
            if len(hits) >= 5:
                items = []
                for li in hits:
                    a  = li.find("a")
                    dt = li.find("i", class_="date")
                    items.append({
                        "title": a.get_text(strip=True),
                        "url":   self.site + a["href"],
                        "date":  dt.get_text(strip=True) if dt else "",
                    })
                return items
        return []

    def _total_pages(self, soup: BeautifulSoup) -> int:
        """从第 1 页的分页组件中读取总页数。"""
        pager = soup.find("ul", class_="yiiPager")
        if not pager:
            return 1
        last_a = pager.find("li", class_="last")
        last_a = last_a.find("a") if last_a else None
        if not last_a:
            return 1
        m = re.search(r"_(\d+)\.html$", last_a["href"])
        return int(m.group(1)) if m else 1

    # ── 内容获取 ───────────────────────────────────────────────────────────────

    def _fetch_soup(self, session: requests.Session, url: str,
                    timeout: int = 15) -> BeautifulSoup | None:
        try:
            r = session.get(url, headers=self.headers, timeout=timeout)
            r.raise_for_status()
            return BeautifulSoup(r.content, "lxml")
        except Exception as e:
            print(f"  [WARN] GET 失败 {url}: {e}", flush=True)
            return None

    def fetch_detail_text(self, session: requests.Session, url: str) -> str:
        """抓取详情页，返回 .txt 容器的纯文本（失败时返回空串）。"""
        soup = self._fetch_soup(session, url, timeout=20)
        if not soup:
            return ""
        node = soup.select_one(self.content_sel)
        return node.get_text("\n") if node else ""

    # ── 法律名称提取（court.gov.cn 专用规则）──────────────────────────────────

    def extract_law_name(self, raw_text: str, fallback_title: str = "") -> str:
        """
        从详情页正文提取真实法律名称（非新闻稿标题）。

        策略：
        1. 正文开头段的 《最高人民…》 书名号内容（最可靠）
        2. 独立标题行：以"最高人民"开头、无动宾结构动词的短行
        3. 兜底：从列表页标题的《》中提取
        """
        # 策略 1：正文开头《最高人民...》
        m = re.search(r"《(最高人民[^》]{5,80})》", raw_text)
        if m:
            return m.group(1).strip()

        # 策略 2：独立标题行
        _VERBS = ("发布", "通知", "已于", "公告", "印发", "就", "向", "决定")
        for line in raw_text.split("\n"):
            line = line.strip()
            if (re.match(r"^(最高人民法院|最高人民检察院).{5,80}$", line)
                    and not any(v in line for v in _VERBS)):
                return line

        # 策略 3：列表页标题《》兜底
        m2 = re.search(r"《([^》]+)》", fallback_title)
        return m2.group(1).strip() if m2 else fallback_title.strip()

    # ── 高级 crawl() ─────────────────────────────────────────────────────────

    def iter_items(self, session: requests.Session,
                   full: bool = False,
                   lookback_days: int = 30,
                   keyword: str = "") -> Iterator[dict]:
        """
        生成器：逐页获取列表，对每个符合条件的条目 yield dict（含 title/url/date/pub）。
        full=True 时忽略日期窗口，遍历全部分页。
        keyword 非空时只 yield 标题含该关键词的条目。
        """
        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)

        # 第 1 页（同时读取总页数）
        soup1 = self._fetch_soup(session, self.list_page_url(1))
        if not soup1:
            raise SourceUnavailableError("列表首页获取失败（网络或站点不可用）")

        total = self._total_pages(soup1)
        print(f"[INFO] court.gov.cn 司法解释列表共 {total} 页", flush=True)

        for page_no in range(1, total + 1):
            if page_no == 1:
                items = self._parse_list_page(soup1)
            else:
                soup = self._fetch_soup(session, self.list_page_url(page_no))
                items = self._parse_list_page(soup) if soup else []

            if not items:
                if page_no == 1:
                    raise SourceUnavailableError(
                        "列表首页解析不到任何条目——页面结构可能已变更")
                break

            page_in_window = False
            for it in items:
                try:
                    pub = datetime.date.fromisoformat(it["date"])
                except (ValueError, TypeError):
                    pub = None

                if pub and pub >= cutoff:
                    page_in_window = True

                if not full and not (pub and pub >= cutoff):
                    continue
                if keyword and keyword not in it["title"]:
                    continue

                yield {**it, "pub": str(pub) if pub else it["date"]}

            print(f"  列表页 {page_no}/{total}", flush=True)

            if not full and not page_in_window:
                print(f"  [STOP] 已超出 {lookback_days} 天时间窗口", flush=True)
                break

            if page_no < total:
                time.sleep(0.4)

    def crawl(
        self,
        session: requests.Session,
        *,
        full: bool = False,
        lookback_days: int = 30,
        keyword: str = "",
    ) -> list[dict]:
        """
        高级接口：爬取符合条件的所有司法解释，返回标准 patch 列表。

        patch 格式与 LawDBMerger.merge() / applyIncremental 接口完全兼容。
        """
        items = list(self.iter_items(session, full=full,
                                     lookback_days=lookback_days, keyword=keyword))
        print(f"\n[INFO] 符合条件的条目: {len(items)} 条，开始抓取详情页…\n",
              flush=True)

        all_patches: list[dict] = []
        skipped = 0
        fetch_failed = 0

        for idx, it in enumerate(items, 1):
            print(f"  [{idx:3d}/{len(items)}] {it['title'][:62]}", flush=True)

            raw_text = self.fetch_detail_text(session, it["url"])
            if not raw_text:
                print("    └─ 详情页获取失败，跳过", flush=True)
                skipped += 1
                fetch_failed += 1
                continue

            law_name = self.extract_law_name(raw_text, it["title"])
            articles = parse_articles(raw_text)

            if not articles:
                print("    └─ 未提取到条文（新闻稿或目录型页面），跳过", flush=True)
                skipped += 1
                continue

            patches = build_patches(
                law_name, articles, it["pub"],
                law_type=self.law_type,
            )
            all_patches.extend(patches)
            print(f"    └─ {law_name[:48]}  {len(articles)} 条 → 累计 {len(all_patches)}",
                  flush=True)

            time.sleep(self.request_delay)

        # 条目全部在"详情页获取"这一步失败 = 网络/反爬层面的故障，不是内容问题
        # （个别条目因是新闻稿/目录页被跳过属正常，不计入此判断）
        if items and fetch_failed == len(items):
            raise SourceUnavailableError(
                f"{len(items)} 个条目的详情页全部获取失败")

        print(f"\n[CRAWL] 完成: {len(all_patches)} 条条文  跳过: {skipped}", flush=True)
        return all_patches
