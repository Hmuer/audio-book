"""上架包导出：把一期的音频 + 文案 + 元数据打成 ZIP。

为什么需要：喜马拉雅「音频批量上传」API 只对企业开发者开放（个人开发者仅 H5），
所以个人主体只能走创作中心手动上传。上架包把一次上传需要的东西都准备好，
把「每天 10 分钟找文件/写文案」压到「拖一个 ZIP、点几下」。

包里内容：
    科技前沿日报_YYYY-MM-DD.mp3     统一命名的音频
    标题.txt / 简介.txt / 标签.txt  直接复制粘贴
    节目简介.md                     含信源链接的完整简介
    元数据.json                     机器可读（时长/音色/信源/日期）
    上传说明.txt                    喜马拉雅创作中心上传步骤 + 注意事项
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from typing import Any

from .constants import TOPIC_TERMS, show_config
from .store import AUDIO_NAME, SUBTITLES_NAME, issue_dir
from . import subtitles


def _plain_markdown(text: str) -> str:
    """把简介 markdown 转成可粘贴的纯文本（保留链接 URL）。"""
    import re

    out = re.sub(r"^#{1,6}\s*", "", text or "", flags=re.MULTILINE)
    out = re.sub(r"^>\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"^[-*]\s+", "· ", out, flags=re.MULTILINE)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1（\2）", out)
    return out.strip() + "\n"


def suggest_tags(meta: dict[str, Any], limit: int = 10) -> list[str]:
    """标签建议：固定品类词 + 当期选题里出现频率最高的主题词。"""
    base = ["科技", "人工智能", "科技前沿", "每日资讯"]
    counts: dict[str, int] = {}
    for topic in meta.get("topics") or []:
        text = f"{topic.get('title') or ''} {topic.get('summary') or ''}".lower()
        for term in TOPIC_TERMS:
            if term.lower() in text:
                counts[term] = counts.get(term, 0) + 1
    hot = [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    tags: list[str] = []
    for tag in base + hot:
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def _safe_name(title: str, fallback: str) -> str:
    name = (title or "").strip() or fallback
    for ch in '\\/:*?"<>|\r\n\t':
        name = name.replace(ch, "_")
    return name[:60].strip() or fallback


def _upload_title(show_title: str, date: str, title: str) -> str:
    """上架标题（= 音频文件名）：品牌 + 日期 + 主题，控制在平台 40 字内。

    喜马拉雅导入音频后拿文件名当标题，所以这里就是最终标题的默认值。
    若内部标题已带品牌前缀，先去重避免"科技前沿日报：科技前沿日报…"。
    """
    topic = (title or "").strip()
    for prefix in (show_title, f"《{show_title}》"):
        if prefix and topic.startswith(prefix):
            topic = topic[len(prefix):].lstrip(" |｜:：-_—·")
            break
    topic = topic.strip() or "科技前沿"
    for candidate in (
        f"{show_title} {date}｜{topic}",
        f"{date}｜{topic}",
    ):
        if len(candidate) <= 40:
            return candidate
    room = max(4, 40 - len(f"{date}｜"))
    return f"{date}｜{topic[:room]}"


def _upload_guide(date: str, mp3_name: str, srt_name: str) -> str:
    return f"""科技前沿日报 · 上架说明（{date}）

一、喜马拉雅创作中心（手动上传，约 1–2 分钟）
1. 打开 https://www.ximalaya.com/ 登录后进入「创作中心」；
2. 选择已有专辑（节目），点「上传声音」；
3. 音频选择本包里的「{mp3_name}」——注意：喜马拉雅会用**文件名**作为节目标题，
   本包已把文件名设成本期标题（可在上传页再微调）；
4. 简介粘贴「简介.txt」（保留信源链接版见「节目简介.md」）；
5. 标签参考「标签.txt」，按平台限制挑选；
6. 分类选择「科技 / 资讯」附近的热门分类（按后台实际选项为准）；
7. 「是否 AI 合成」按实际情况选择（本节目为 AI 辅助制作，应如实勾选）；
8. 字幕文件选择本包里的「{srt_name}」；
9. 提交审核。

二、注意事项
- 封面 3000×3000（JPG/PNG，无二维码、无联系方式），首次上架时设置，之后沿用；
- 标题不含日期、不夸张、不制造悬念；
- 音频开头已含「AI 辅助制作，内容经人工审核」的中性声明；
- 本包由系统自动生成，元数据见「元数据.json」。

三、想改成全自动？
喜马拉雅「音频批量上传」API 仅对企业开发者开放（个人开发者仅 H5）。
若有企业主体入驻开放平台（open.ximalaya.com），可把 App Key/Secret 配置到服务器，
系统即可自动上传；否则请保留本手动流程。
"""


def build_package(date: str, meta: dict[str, Any], *, shownotes_md: str, script_md: str) -> bytes:
    """生成上架包 ZIP 字节（纯内存，不写临时文件）。"""
    base = issue_dir(date)
    audio_path = base / AUDIO_NAME
    if not audio_path.is_file():
        raise FileNotFoundError("本期还没有音频")
    show = show_config()
    title = str(meta.get("title") or f"{show['title']}_{date}")
    upload_title = _upload_title(show["title"], date, title)
    safe_title = _safe_name(upload_title, f"{show['title']} {date}")
    mp3_name = f"{safe_title}.mp3"
    srt_name = f"{safe_title}.srt"

    tags = suggest_tags(meta)
    meta_json = {
        "show": show,
        "date": date,
        "title": title,
        "upload_title": upload_title,
        "duration_ms": meta.get("duration_ms"),
        "voice": meta.get("voice"),
        "item_count": meta.get("item_count"),
        "topic_count": len(meta.get("topics") or []),
        "topics": [
            {"title": t.get("title"), "source": t.get("source")}
            for t in (meta.get("topics") or [])
        ],
        "tags": tags,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(mp3_name, audio_path.read_bytes())
        srt_path = base / SUBTITLES_NAME
        if srt_path.is_file():
            srt_text = srt_path.read_text(encoding="utf-8")
        else:
            srt_text = subtitles.build_from_script(script_md, int(meta.get("duration_ms") or 0))
        if srt_text:
            zf.writestr(srt_name, srt_text)
        zf.writestr("标题.txt", upload_title + "\n")
        zf.writestr("简介.txt", _plain_markdown(shownotes_md))
        zf.writestr("节目简介.md", shownotes_md or "")
        zf.writestr("标签.txt", "、".join(tags) + "\n")
        zf.writestr("元数据.json", json.dumps(meta_json, ensure_ascii=False, indent=2))
        zf.writestr("上传说明.txt", _upload_guide(date, mp3_name, srt_name))
        if script_md:
            zf.writestr("口播稿.txt", script_md + "\n")
    return buf.getvalue()
