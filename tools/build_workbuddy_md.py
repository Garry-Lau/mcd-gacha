"""把 WorkBuddy 导出的对话数据转成 Markdown 对话记录（供 workbuddy.md 使用）。

为什么要脚本化
--------------
参赛要求 ``workbuddy.md`` 必须「保留完整的 WorkBuddy 对话上下文」。手工粘贴
无法保证完整，也不可复查。本脚本直接消费平台导出的 ``conversation-data.json``，
机械地把每一轮对话转成 Markdown —— **不对对话内容做任何改写**（只对凭据打码）。

用法
----
::

    python tools/build_workbuddy_md.py conversation-data.json \\
        --prologue workbuddy.md --out workbuddy.md

- ``conversation-data.json``：从 WorkBuddy 分享页的静态资源地址取得
  （页面 HTML 里的 ``window.__PUBLISH_BOOTSTRAP__`` 会给出
  ``.../page/<nodeId>/<hash>/conversation-data.json``）。
- ``--prologue``：作为「第一部分」插入的既有 Markdown（可选）。脚本会读取它，
  再写入 ``--out``，因此可以原地更新。
- ``--reasoning/--results``：是否收录模型内部推理与工具原始返回（默认都不收录，
  见 ``--help``）。

收录范围
--------
- 用户消息：原文完整收录（含首轮任务书的长文本）。
- 助手消息：所有**可见文本**完整收录。
- 工具调用：逐条记录工具名 + 目的/命令摘要 + 目标文件（默认不含返回体）。
- 默认**不收录**内部推理（reasoning）与工具返回体，避免仓库里出现数 MB 的对话原文；
  需要完全原始版本时加 ``--reasoning --results``。
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: 导出数据中的时间戳是毫秒 epoch（UTC），展示统一换算到 UTC+8。
TZ = timezone(timedelta(hours=8))

#: 需要打码的凭据形态：券码（MCD 开头的长串）与疑似 Token。
_MASK_PATTERNS = [
    (re.compile(r"MCD[A-Z0-9]{8,}"), lambda m: m.group(0)[:8] + "****"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), lambda m: m.group(1) + "****"),
]


def mask(text: str) -> str:
    """对凭据做打码，其余内容原样保留。"""
    for pat, repl in _MASK_PATTERNS:
        text = pat.sub(repl, text)
    return text


def ts(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")


def one_line(text: str, limit: int = 100) -> str:
    """压成单行、截断，供工具轨迹使用。"""
    text = mask(re.sub(r"\s+", " ", str(text or "")).strip())
    text = text.replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def user_text(content: list) -> list[str]:
    """取用户消息里的全部可见文本（长文本藏在 resource_link._meta.fullText）。"""
    out: list[str] = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            out.append(block.get("text") or "")
        elif kind == "resource_link":
            meta = block.get("_meta") or {}
            full = meta.get("fullText")
            if not full and block.get("uri"):
                full = urllib.parse.unquote(block["uri"].replace("longtext://", ""))
            out.append(full or block.get("name") or "")
        else:
            out.append(f"[{kind}]")
    return [t for t in out if t.strip()]


def render(data: dict, *, with_reasoning: bool = False, with_results: bool = False) -> tuple[str, str]:
    msgs = data.get("messages") or []
    lines: list[str] = []
    turns = 0
    n_reason = n_tool = 0

    for msg in msgs:
        role = msg.get("messageType")
        content = msg.get("content") or []
        # 无内容的消息（如上下文压缩这类时间线事件）不占篇幅
        if not content:
            continue

        if role == "user":
            turns += 1
            lines.append(f"## 第 {turns} 轮 · 用户 · {ts(msg.get('createTime'))}")
            lines.append("")
            for text in user_text(content):
                lines.append(mask(text).rstrip())
                lines.append("")

        else:
            head = f"## 第 {turns} 轮 · WorkBuddy · {ts(msg.get('createTime'))}"
            lines.append(head)
            lines.append("")
            for block in content:
                kind = block.get("type")
                if kind == "text":
                    lines.append(mask(block.get("text") or "").rstrip())
                    lines.append("")
                elif kind == "reasoning" and with_reasoning:
                    n_reason += 1
                    lines.append("<details><summary>内部推理</summary>")
                    lines.append("")
                    lines.append(mask(block.get("text") or "").rstrip())
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")
            tools = [b["tool"] for b in content if b.get("type") == "tool-call"]
            if tools:
                n_tool += len(tools)
                lines.append(f"<details><summary>工具执行轨迹（{len(tools)} 次调用）</summary>")
                lines.append("")
                for idx, tool in enumerate(tools, 1):
                    args = tool.get("args") or {}
                    what = (
                        args.get("description")
                        or args.get("command")
                        or args.get("file_path")
                        or args.get("query")
                        or args.get("prompt")
                        or ""
                    )
                    target = args.get("file_path") or args.get("path") or ""
                    row = f"{idx}. `{tool.get('name')}` · {one_line(what)}"
                    if target and target not in row:
                        row += f" → `{one_line(target, 80)}`"
                    if tool.get("status") and tool["status"] != "executed":
                        row += f" · 状态={tool['status']}"
                    lines.append(row)
                    if with_results:
                        res = (tool.get("result") or {}).get("text") or ""
                        if res:
                            lines.append("")
                            lines.append("    ```text")
                            for ln in mask(res).rstrip().splitlines():
                                lines.append("    " + ln)
                            lines.append("    ```")
                            lines.append("")
                lines.append("</details>")
                lines.append("")

    note = (
        f"共 {turns} 轮对话、{n_tool} 次工具调用"
        + (f"、{n_reason} 段内部推理" if with_reasoning else "")
        + "。"
    )
    return "\n".join(lines).rstrip() + "\n", note


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="WorkBuddy 导出的 conversation-data.json")
    ap.add_argument("--out", default="-", help="输出 Markdown（默认 stdout）")
    ap.add_argument("--prologue", default=None, help="前置插入的既有 Markdown 文件（作为第一部分）")
    ap.add_argument("--reasoning", action="store_true", help="收录内部推理（体积很大）")
    ap.add_argument("--results", action="store_true", help="收录工具原始返回（体积很大）")
    args = ap.parse_args()

    data = json.loads(Path(args.source).read_text(encoding="utf-8"))
    body, note = render(data, with_reasoning=args.reasoning, with_results=args.results)

    header = [
        "# 第二部分 · 完整对话记录",
        "",
        f"> 来源：WorkBuddy 会话「{data.get('name')}」"
        f"（conversationId `{data['messages'][0].get('conversationId')}`）",
        f"> {note}",
        "> 由 `tools/build_workbuddy_md.py` 机械转换，对话原文未做改写；券码等凭据已打码。",
        "",
        "---",
        "",
    ]

    parts: list[str] = []
    if args.prologue:
        pro = Path(args.prologue).read_text(encoding="utf-8").rstrip()
        # 作为「第一部分」嵌入时整体降一级，保证全文标题层级不出现两个 H1
        pro = re.sub(r"^(#{1,5}) ", lambda m: "#" + m.group(1) + " ", pro, flags=re.M)
        parts += ["# 第一部分 · 协作概述（精炼版）", "", pro, "", "---", ""]
    parts += header + [body]

    out = "\n".join(parts)
    if args.out == "-":
        print(out)
    else:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"[build_workbuddy_md] 已写入 {args.out}（{len(out):,} 字符）· {note}")


if __name__ == "__main__":
    main()
