#!/usr/bin/env python3
"""
merge.py — 法条数据库合并模块
==============================

对外提供两类能力：

① 共享工具（供所有爬虫/导入脚本直接 import）
   - cn_to_int(s)           中文数字 → 整数
   - make_law_id(raw_name)  派生 lawId（去书名号、保留序数括号）
   - parse_articles(text)   逐行提取"第X条"条文
   - build_patches(...)     生成 latest_laws.json patch 记录

② LawDBMerger 类（将任意来源的 patches 合并写入 laws-database.json）
   - merger = LawDBMerger(db_path)
   - stats  = merger.merge(patches)   # 内存操作，可调用多次
   - merger.save()                    # 写回磁盘 + 升级 version

独立 CLI 使用：
   python merge.py latest_laws.json
   python merge.py my_data.json --db ../../data/laws-database.json
   python merge.py latest_laws.json --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── 默认 DB 路径（相对本文件）────────────────────────────────────────────────────
_DEFAULT_DB = Path(__file__).parents[1] / "data" / "laws-database.json"

# ═══════════════════════════════════════════════════════════════════════════════
# 共享工具函数
# ═══════════════════════════════════════════════════════════════════════════════

_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNIT  = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn_to_int(s: str) -> int:
    """将中文数字字符串（或阿拉伯数字字符串）转为整数。"""
    if re.fullmatch(r"\d+", s):
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in _DIGIT:
            cur = _DIGIT[ch]
        elif ch in _UNIT:
            if ch == "十" and cur == 0:
                cur = 1          # "十三" → 13
            total += cur * _UNIT[ch]
            cur = 0
    return total + cur


_ORDINAL_RE = re.compile(r"^[零一二三四五六七八九十百千万\d]+$")


def make_law_id(raw_name: str) -> str:
    """
    从法律名称派生 lawId：
    - 去掉书名号 《》〈〉（与 JS normalize() 一致）
    - 剥离版次型括号内容，如（2023年修正）
    - 保留纯序数括号，如（一）（四），以区分系列司法解释
    """
    name = re.sub(r"[《》〈〉]", "", raw_name)

    def _keep_ordinal(m: re.Match) -> str:
        return m.group(0) if _ORDINAL_RE.match(m.group(1).strip()) else ""

    name = re.sub(r"[（(]([^）)]*)[）)]", _keep_ordinal, name).strip()
    return name or raw_name


# ── 逐行条文解析器（适用于所有标准中文法律文本）───────────────────────────────
# 行首 "第X条" 触发新条文；行内引用（如"公司法第X条"）不匹配，因为没有缩进
_ART_HEAD_RE = re.compile(
    r"^[ \t　]*第\s*([零〇一二三四五六七八九十百千万\d]+)\s*条"
    r"(?:[ \t　]*[【\[（(][^】\]）)]{0,20}[】\]）)])?[ \t　]*(.*)"
)


def parse_articles(text: str) -> list[dict]:
    """
    逐行扫描原始文本，提取"第X条"条文列表。

    - 行首出现 第X条 → 开启新条文（其后的内容行追加到当前条文）
    - 行内引用（如"依据公司法第三十三条"）不会触发，因为 _ART_HEAD_RE
      要求 第X条 必须出现在行首（前面只允许空白字符）
    - 返回 list[{articleNumber, citation, content}]
    """
    results: list[dict] = []
    cur_num_str: str | None = None
    cur_lines: list[str] = []

    def _flush() -> None:
        if cur_num_str is None:
            return
        content = re.sub(r"\s+", " ", " ".join(cur_lines)).strip()
        if len(content) >= 8:
            num = cn_to_int(cur_num_str)
            if num > 0:
                results.append({
                    "articleNumber": num,
                    "citation":      f"第{cur_num_str}条",
                    "content":       content,
                })

    for line in text.split("\n"):
        m = _ART_HEAD_RE.match(line)
        if m:
            _flush()
            cur_num_str = m.group(1).strip()
            cur_lines   = [m.group(2).strip()] if m.group(2) else []
        elif cur_num_str is not None:
            stripped = line.strip()
            if stripped:
                cur_lines.append(stripped)

    _flush()
    return results


def build_patches(
    law_name: str,
    articles: list[dict],
    pub_date: str,
    *,
    law_type: str = "interpretation",
    law_aliases: list[str] | None = None,
    law_status: str = "现行有效",
) -> list[dict]:
    """
    将 (法律名, 条文列表, 发布日期) 转为 latest_laws.json patch 格式。
    patch 格式与插件 applyIncremental 接口完全匹配。
    """
    law_id = make_law_id(law_name)
    return [
        {
            "lawId":        law_id,
            "lawType":      law_type,
            "lawName":      law_name,
            "lawAliases":   law_aliases or [],
            "lawEffective": pub_date,
            "lawVersion":   "",
            "lawStatus":    law_status,
            "number":       a["articleNumber"],
            "citation":     a["citation"],
            "text":         a["content"],
            "publishDate":  pub_date,
        }
        for a in articles
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# LawDBMerger — 合并引擎
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MergeStats:
    new_laws:          int = 0
    new_articles:      int = 0
    updated_articles:  int = 0

    def __str__(self) -> str:
        return (f"+{self.new_laws} 部新法律  "
                f"+{self.new_articles} 条新条文  "
                f"↑{self.updated_articles} 条覆盖更新")


class LawDBMerger:
    """
    将任意来源的 patches 合并进 laws-database.json。

    用法：
        merger = LawDBMerger()                    # 使用默认 DB 路径
        merger = LawDBMerger("/path/to/db.json")  # 指定路径
        stats  = merger.merge(patches)            # 可多次调用，累计合并
        merger.save()                             # 写磁盘 + 升级 version
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB
        if not self.db_path.exists():
            raise FileNotFoundError(f"laws-database.json 不存在: {self.db_path}")
        self._db    = json.loads(self.db_path.read_text(encoding="utf-8"))
        self._dirty = False

        # 构建 lawId → law_dict 的快速查找表
        laws = self._db.setdefault("laws", [])
        self._law_map: dict[str, dict] = {law["id"]: law for law in laws}

    # ── 合并入口 ───────────────────────────────────────────────────────────────
    def merge(self, patches: list[dict]) -> MergeStats:
        """
        将 patches 合并进内存中的 DB 镜像（不写磁盘）。
        可以连续调用多次，最后统一 save()。

        去重规则：(lawId, articleNumber) 相同 → 覆盖更新；否则追加。
        """
        stats = MergeStats()
        grouped: dict[str, list[dict]] = {}
        for p in patches:
            grouped.setdefault(p["lawId"], []).append(p)

        for law_id, pts in grouped.items():
            law_entry = self._get_or_create_law(law_id, pts[0], stats)
            arts      = law_entry.setdefault("articles", [])
            num_index = {a["number"]: i for i, a in enumerate(arts)}

            for p in pts:
                num = p["number"]
                art = {"number": num, "citation": p["citation"], "text": p["text"]}
                if num in num_index:
                    arts[num_index[num]] = art
                    stats.updated_articles += 1
                else:
                    arts.append(art)
                    num_index[num] = len(arts) - 1
                    stats.new_articles += 1

            arts.sort(key=lambda a: a["number"])

        self._dirty = True
        return stats

    # ── 写盘 ───────────────────────────────────────────────────────────────────
    def save(self, bump_version: bool = True) -> str:
        """
        将内存镜像写回磁盘。
        bump_version=True（默认）：将 laws-database.json 的 version 更新为今日日期，
        从而触发插件在下次启动时自动重建 IndexedDB。
        返回最终写入的 version 字符串。
        """
        if bump_version:
            today = datetime.date.today().isoformat()
            self._db["version"] = today
        self._db["laws"] = list(self._law_map.values())
        # 原子写入：14MB 主库若在写到一半时崩溃/磁盘满，会留下无法解析的
        # 截断文件且没有备份；先写临时文件再 os.replace 保证要么旧要么新
        tmp_path = self.db_path.with_suffix(self.db_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(self._db, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.db_path)
        self._dirty = False
        return str(self._db.get("version", ""))

    # ── 预览（dry-run）────────────────────────────────────────────────────────
    def preview(self, patches: list[dict]) -> MergeStats:
        """不修改任何状态，仅统计若执行 merge 会产生多少变更。"""
        stats = MergeStats()
        grouped: dict[str, list[dict]] = {}
        for p in patches:
            grouped.setdefault(p["lawId"], []).append(p)

        for law_id, pts in grouped.items():
            if law_id not in self._law_map:
                stats.new_laws += 1
                stats.new_articles += len(pts)
            else:
                existing = {a["number"] for a in self._law_map[law_id].get("articles", [])}
                for p in pts:
                    if p["number"] in existing:
                        stats.updated_articles += 1
                    else:
                        stats.new_articles += 1
        return stats

    # ── 内部：取或新建 law 条目 ────────────────────────────────────────────────
    def _get_or_create_law(self, law_id: str, sample_patch: dict,
                           stats: MergeStats) -> dict:
        if law_id in self._law_map:
            return self._law_map[law_id]

        entry: dict = {
            "id":       law_id,
            "name":     sample_patch.get("lawName", law_id),
            "aliases":  sample_patch.get("lawAliases") or [],
            "effective": sample_patch.get("lawEffective", ""),
            "version":  sample_patch.get("lawVersion", ""),
            "status":   sample_patch.get("lawStatus", "现行有效"),
            "type":     sample_patch.get("lawType", "interpretation"),
            "articles": [],
        }
        self._law_map[law_id] = entry
        self._db["laws"].append(entry)
        stats.new_laws += 1
        return entry


# ═══════════════════════════════════════════════════════════════════════════════
# 独立 CLI：python merge.py <patches.json> [选项]
# ═══════════════════════════════════════════════════════════════════════════════

def _cli() -> None:
    ap = argparse.ArgumentParser(
        description="将 patches JSON 文件合并进 laws-database.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python merge.py latest_laws.json
  python merge.py my_data.json --db ../../data/laws-database.json
  python merge.py latest_laws.json --dry-run
""",
    )
    ap.add_argument("patches_file", help="包含 patches 数组的 JSON 文件路径")
    ap.add_argument("--db",       default=None, help="laws-database.json 路径（默认自动定位）")
    ap.add_argument("--dry-run",  action="store_true", help="只预览变更，不写磁盘")
    ap.add_argument("--no-bump",  action="store_true", help="不升级 version（慎用）")
    args = ap.parse_args()

    pf = Path(args.patches_file)
    if not pf.exists():
        sys.exit(f"[ERROR] 文件不存在: {pf}")

    payload = json.loads(pf.read_text(encoding="utf-8"))
    patches = payload if isinstance(payload, list) else payload.get("patches", [])
    if not patches:
        sys.exit("[ERROR] 未找到 patches 数据")

    print(f"[merge] 读取 patches: {len(patches)} 条  来自: {pf.name}")

    try:
        merger = LawDBMerger(args.db)
    except FileNotFoundError as e:
        sys.exit(f"[ERROR] {e}")

    if args.dry_run:
        stats = merger.preview(patches)
        print(f"[dry-run] 预计变更: {stats}")
        return

    stats = merger.merge(patches)
    ver   = merger.save(bump_version=not args.no_bump)
    print(f"[Done] {stats}  version → {ver}")
    print(f"[Done] 已写入: {merger.db_path}")


if __name__ == "__main__":
    _cli()
