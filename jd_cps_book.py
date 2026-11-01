"""可选图书推广模块：会场长期链接置顶 + 每日从本地 Numbers 维护表轮换一本。

如未配置环境变量 JD_CPS_VENUE_URL 且不存在本地维护表，自动静默返回空字符串，不影响开源默认发布。
"""
from __future__ import annotations

import html
import os
import sys
from datetime import date
from pathlib import Path

TABLE_PATH = Path(__file__).resolve().parent / "京东CPS" / "图书短链维护表.numbers"

VENUE_URL = os.environ.get("JD_CPS_VENUE_URL", "").strip()


def build_venue_html() -> str:
    """返回置顶的图书会场入口 <p> 块（仅在配置了 JD_CPS_VENUE_URL 时输出）。"""
    venue_url = os.environ.get("JD_CPS_VENUE_URL", VENUE_URL).strip()
    if not venue_url:
        return ""
    return (
        f'<p>🛒 AI 好书一站购｜<a href="{venue_url}">京东人工智能图书会场</a>'
        "（每期荐书都在这，长期有效）</p>"
    )

# 表列序：0序号 1书名 2作者 3一句话推荐语 4话题标签 5京东链接 6转链日期 7状态 8备注
_COL_TITLE, _COL_BLURB, _COL_LINK, _COL_STATUS = 1, 3, 5, 7


def _load_books() -> list[dict[str, str]]:
    from numbers_parser import Document

    table = Document(TABLE_PATH).sheets["书单短链"].tables[0]
    books: list[dict[str, str]] = []
    for row in range(1, table.num_rows):
        def cell(col: int) -> str:
            value = table.cell(row, col).value
            return str(value).strip() if value is not None else ""

        title = cell(_COL_TITLE)
        if not title or title.startswith("【示例】") or cell(0) == "示例":
            continue
        link = cell(_COL_LINK)
        if cell(_COL_STATUS) == "停用" or not link.lower().startswith("http"):
            continue
        books.append({"title": title, "blurb": cell(_COL_BLURB), "link": link})
    return books


def build_daily_book_html(day: date | None = None) -> str:
    """返回当日荐书的前置 <p> 块；表缺失/无可用书/解析失败一律返回空串。"""
    if not TABLE_PATH.exists():
        return ""
    try:
        books = _load_books()
        if not books:
            return ""
        book = books[(day or date.today()).toordinal() % len(books)]
        # 京东长链含 `|`，原样贴进 shownotes 会截断链接识别，转义为 %7C
        url = html.escape(book["link"].replace("|", "%7C"), quote=True)
        title = html.escape(book["title"])
        blurb = html.escape(book["blurb"])
        tail = f"——{blurb}" if blurb else ""
        return f'<p>📚 今日荐书｜<a href="{url}">《{title}》</a>{tail}</p>'
    except Exception as exc:
        print(f"[jd_cps_book WARNING] 今日荐书生成失败，跳过: {exc}", file=sys.stderr)
        return ""


if __name__ == "__main__":
    print(build_venue_html())
    print(build_daily_book_html())
