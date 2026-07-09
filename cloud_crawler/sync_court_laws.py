#!/usr/bin/env python3
"""
sync_court_laws.py — 最高法爬虫主入口
======================================

将 CourtGovSource（爬虫）与 LawDBMerger（合并引擎）串联的薄 CLI。

运行方式：
  # 近 30 天增量（默认）
  python sync_court_laws.py

  # 全量抓取所有 20 页
  python sync_court_laws.py --full

  # 全量 + 关键词过滤
  python sync_court_laws.py --full --keyword 公司法

  # 全量 + 写回 laws-database.json
  python sync_court_laws.py --full --merge-db

  # 全量 + 过滤 + 写回（初次建库 / 专项补充）
  python sync_court_laws.py --full --keyword 公司法 --merge-db

输出：
  cloud_crawler/latest_laws.json   — 供云端同步和手动检查
  data/laws-database.json          — 仅在 --merge-db 时更新（触发插件 IDB 重建）
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import requests

from court_source import CourtGovSource, SourceUnavailableError
from merge import LawDBMerger

OUTPUT_FILE = Path(__file__).with_name("latest_laws.json")
DB_FILE     = Path(__file__).parents[1] / "data" / "laws-database.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="court.gov.cn 司法解释爬虫")
    ap.add_argument("--full",     action="store_true",
                    help="抓取全部分页（历史回填）")
    ap.add_argument("--days",     type=int, default=30,
                    help="增量模式回溯天数（默认 30）")
    ap.add_argument("--keyword",  default="",
                    help="标题关键词过滤，留空=全部")
    ap.add_argument("--merge-db", action="store_true", dest="merge_db",
                    help="爬取后将条文写回 data/laws-database.json")
    ap.add_argument("--delay",    type=float, default=0.8,
                    help="详情页请求间隔秒数（默认 0.8）")
    args = ap.parse_args()

    mode = "全量" if args.full else f"增量({args.days}天)"
    print(f"\n[START] 模式={mode}  关键词={args.keyword or '（全部）'}\n",
          flush=True)

    source  = CourtGovSource(request_delay=args.delay)
    session = requests.Session()

    # 故障（网络/结构变更/详情页全灭）与"真没有新条文"必须分开：
    # 前者退出码 2 → CI 变红报警；后者退出码 0 且不动 latest_laws.json。
    # 二者若混为一谈，故障就会被伪装成"今天没有新法"而无人察觉。
    try:
        patches = source.crawl(
            session,
            full=args.full,
            lookback_days=args.days,
            keyword=args.keyword,
        )
    except SourceUnavailableError as e:
        print(f"\n[FATAL] 数据源不可用：{e}（退出码 2）", flush=True)
        sys.exit(2)

    if not patches:
        print("\n[INFO] 数据源正常，时间窗口内无新条文；"
              "保持现有 latest_laws.json 不变（退出码 0）", flush=True)
        return

    today   = datetime.date.today().isoformat()
    payload = {"version": today, "patches": patches}
    # 原子写入：避免中途崩溃留下截断的 JSON
    tmp = OUTPUT_FILE.with_suffix(OUTPUT_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, OUTPUT_FILE)
    print(f"\n[Save] {today} — {len(patches)} 条条文 → {OUTPUT_FILE.name}",
          flush=True)

    if args.merge_db and patches:
        merger = LawDBMerger(DB_FILE)
        stats  = merger.merge(patches)
        ver    = merger.save()
        print(f"[DB]   {stats}  version → {ver}", flush=True)
        print(f"[DB]   已写入: {merger.db_path}", flush=True)


if __name__ == "__main__":
    main()
