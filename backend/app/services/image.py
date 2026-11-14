"""图片生成：OpenAI / Stability 可插拔；无密钥时用 Pillow 生成带标注占位图。

- 生成/上传都落到 storage/images；body.path 存相对 storage_dir 的路径（URL 前缀 /images/）。
- 传入 item_id 表示「原地重做」：替换该图片，旧版本进 meta.history。
- 上传导入：source=upload，文件名加 uuid 前缀防冲突。
"""
from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import ContentItem, Course, OutlineNode
from app.services.kb import attach_vault_write


def generate_image(db: Session, course: Course, prompt: str, size: str,
                   item_id: str | None = None, node_id: str | None = None,
                   *, text_lang: str = "none", overlay_text: str = "",
                   overlay_pos: str = "top", model: str | None = None) -> ContentItem:
    settings = get_settings()
    width, height = (int(v) for v in size.split("x"))
    use_model = (model or "").strip() or settings.image_model
    want_text = text_lang in ("zh", "en")
    overlay_text = (overlay_text or "").strip()[:60]

    node = None
    if node_id:
        node = db.get(OutlineNode, node_id)
        if node is None or node.outline is None or node.outline.course_id != course.id:
            raise ValueError("所选章节不属于当前课程，请重新选择")
    if want_text and not overlay_text:
        overlay_text = ((node.title if node is not None else course.title) or "")[:60]

    prompt = (prompt or "").strip()
    auto = not prompt
    if auto:
        prompt = _auto_prompt(db, course, node)
    if want_text and overlay_text:
        pos_hint = {"top": "顶部", "center": "中部", "bottom": "底部"}.get(overlay_pos, "顶部")
        prompt = (
            f"{prompt}\n"
            "（重要：这是一张“无文字底图”，画面里严禁出现任何文字、字母、数字或标注；"
            f"{pos_hint}区域请留出干净空白作为标题区，"
            "标题文字会由后期排版统一叠加。请只绘制插画/场景本身，不要写字。）"
        ).strip()

    if settings.image_provider == "openai" and settings.openai_api_key:
        file_path = _from_openai(settings, prompt, size, width, height)
    elif settings.image_provider == "stability" and settings.stability_api_key:
        file_path = _from_stability(settings, prompt, size, width, height)
    elif settings.image_provider == "openrouter" and settings.openrouter_api_key:
        file_path = _from_openrouter(settings, prompt, size, width, height, model=use_model)
    else:
        file_path = _placeholder(prompt, width, height)

    if want_text and overlay_text:
        file_path = _overlay_text(file_path, overlay_text, overlay_pos=overlay_pos)

    rel = str(file_path.relative_to(settings.storage_dir)).replace("\\", "/")
    body = {"path": rel, "prompt": prompt, "size": size, "source": "generate"}
    if want_text:
        body.update({"text_lang": text_lang, "overlay_text": overlay_text, "overlay_pos": overlay_pos})
    if use_model != settings.image_model:
        body["model"] = use_model
    item = _target_item(db, course, item_id, kind="image")
    if item is None:
        item = ContentItem(course_id=course.id, kind="image",
                           node_id=node.id if node else None,
                           title=_image_title(course, node, prompt, auto), body=body)
        db.add(item)
    else:
        history = list(item.meta.get("history") or [])
        history.insert(0, {"body": item.body})
        item.meta = {**item.meta, "history": history[:5]}
        if node is not None:
            item.node_id = node.id
        item.title = _image_title(course, node, prompt, auto)
        item.body = body
    db.commit()
    db.refresh(item)
    try:
        attach_vault_write(db, item, "image", {"image_path": item.body.get("path", "")})
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        pass
    return item


def _image_title(course: Course, node, prompt: str, auto: bool) -> str:
    if auto:
        return f"{node.title} · 配图" if node else f"{course.title} · 整体示意图"
    return prompt[:50]


def _auto_prompt(db: Session, course: Course, node) -> str:
    """留空提示词时，按大纲自动构思配图描述（不额外调 LLM，拼装大纲上下文）。"""
    from app.services.planner import get_outline, list_nodes

    nodes = list_nodes(db, course.id)
    parts = [f"为课程《{course.title}》生成一张信息准确的教学示意图。"]
    if course.subject:
        parts.append(f"学科：{course.subject}。")
    if course.audience:
        parts.append(f"受众：{course.audience}。")
    if node is not None:
        scope = f"本章主题《{node.title}》。"
        if node.objective:
            scope += f"学习目标：{node.objective}。"
        children = [n for n in nodes if n.node_key.startswith(f"{node.node_key}.")]
        if children:
            scope += "涉及知识点：" + "、".join(n.title for n in children[:8]) + "。"
        parts.append(scope)
    else:
        chapters = [n for n in nodes if n.node_type == "chapter"] or nodes[:8]
        if chapters:
            parts.append("覆盖章节：" + "、".join(n.title for n in chapters[:8]) + "。")
        else:
            outline = get_outline(db, course.id)
            if outline and outline.goal:
                parts.append(f"课程目标：{outline.goal}。")
    parts.append("画面要求：扁平插画风、浅色背景，画面中不要出现任何文字、数字或标注"
                 "（文字信息请用文本与流程图承载），可直接用于课堂讲义。")
    return "".join(parts)[:800]


def import_image(db: Session, course: Course, filename: str, data: bytes,
                 item_id: str | None = None, node_id: str | None = None) -> ContentItem:
    """用户上传自己的图片作为课程素材（source=upload）。"""
    settings = get_settings()
    out_dir = settings.storage_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in Path(filename).name if c.isalnum() or c in "._-") or "image.png"
    out = out_dir / f"{uuid.uuid4().hex}_{safe_name}"
    out.write_bytes(data)
    rel = str(out.relative_to(settings.storage_dir)).replace("\\", "/")
    body = {"path": rel, "source": "upload", "original_name": safe_name}
    node = None
    if node_id:
        node = db.get(OutlineNode, node_id)
        if node is None or node.outline is None or node.outline.course_id != course.id:
            raise ValueError("所选章节不属于当前课程，请重新选择")
    item = _target_item(db, course, item_id, kind="image")
    if item is None:
        item = ContentItem(course_id=course.id, kind="image",
                           node_id=node.id if node else None,
                           title=safe_name.rsplit(".", 1)[0][:50], body=body)
        db.add(item)
    else:
        history = list(item.meta.get("history") or [])
        history.insert(0, {"body": item.body})
        item.meta = {**item.meta, "history": history[:5]}
        if node is not None:
            item.node_id = node.id
        item.title = safe_name.rsplit(".", 1)[0][:50]
        item.body = body
    db.commit()
    db.refresh(item)
    try:
        attach_vault_write(db, item, "image", {"image_path": item.body.get("path", "")})
    except Exception:  # noqa: BLE001
        pass
    return item


def _target_item(db: Session, course: Course, item_id: str | None, kind: str) -> ContentItem | None:
    if not item_id:
        return None
    item = db.get(ContentItem, item_id)
    if item is None or item.course_id != course.id or item.kind != kind:
        raise ValueError(f"内容项不存在或类型不符：{item_id}")
    return item


def _from_openai(settings, prompt: str, size: str, width: int, height: int) -> Path:
    resp = httpx.post(
        "https://api.openai.com/v1/images/generations",
        json={"model": "dall-e-3", "prompt": prompt, "size": size, "n": 1},
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        timeout=180,
    )
    resp.raise_for_status()
    b64 = resp.json()["data"][0]["b64_json"]
    return _save(b64, width, height)


def _from_stability(settings, prompt: str, size: str, width: int, height: int) -> Path:
    resp = httpx.post(
        "https://api.stability.ai/v2beta/stable-image/generate/core",
        headers={"Authorization": f"Bearer {settings.stability_api_key}", "Accept": "image/*"},
        data={"prompt": prompt, "aspect_ratio": size, "output_format": "png"},
        timeout=180,
    )
    resp.raise_for_status()
    return _save(base64.b64encode(resp.content).decode(), width, height)


def _from_openrouter(settings, prompt: str, size: str, width: int, height: int,
                     model: str | None = None) -> Path:
    """OpenRouter 多模态图像模型（如 google/gemini-2.5-flash-image）。

    返回的 base64 图片会按请求尺寸用 Pillow 居中裁切适配（模型本身常固定输出
    1024x1024），保证 UI 尺寸与导出一致。
    """
    resp = httpx.post(
        f"{settings.openrouter_base_url}/chat/completions",
        json={
            "model": model or settings.image_model,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": prompt}],
        },
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        timeout=240,
    )
    resp.raise_for_status()
    message = resp.json()["choices"][0]["message"]
    data_url = _extract_image_data_url(message)
    if data_url is None:
        raise RuntimeError("图像模型未返回图片，请换一句更具体的描述后重试")
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return _save_fitted(raw, width, height)


def _extract_image_data_url(message: dict) -> str | None:
    """从多模态回复中提取第一张图片的 data URL（兼容 images / content 两种外观）。"""
    for part in message.get("images") or []:
        if not isinstance(part, dict):
            continue
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            url = image_url.get("url")
            if isinstance(url, str) and url.startswith("data:image/"):
                return url
    for part in message.get("content") or []:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            url = image_url.get("url")
            if isinstance(url, str) and url.startswith("data:image/"):
                return url
    return None


def _save_fitted(raw: bytes, width: int, height: int) -> Path:
    settings = get_settings()
    out_dir = settings.storage_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{uuid.uuid4().hex}.png"
    with Image.open(io.BytesIO(raw)) as src:
        src.load()
        if src.mode != "RGB":
            src = src.convert("RGB")
        ImageOps.fit(src, (width, height), method=Image.Resampling.LANCZOS).save(out, format="PNG")
    return out

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/arial.ttf",
)


def _load_font(size: int):
    for cand in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_by_width(text: str, draw: ImageDraw.ImageDraw, font, max_w: int,
                   limit: int = 5) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines() or [text]:
        rest = raw
        while rest:
            if draw.textlength(rest, font=font) <= max_w:
                lines.append(rest)
                break
            lo, hi = 0, len(rest)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if draw.textlength(rest[:mid], font=font) <= max_w:
                    lo = mid
                else:
                    hi = mid - 1
            if lo == 0:
                lo = 1
            lines.append(rest[:lo])
            rest = rest[lo:]
    return lines[:limit] or [text[:30] or "（无提示词）"]


def _placeholder(prompt: str, width: int, height: int) -> Path:
    """Pillow 占位图：浅色卡片 + 中文提示词标注，深/浅主题下都清晰可见。"""
    img = Image.new("RGB", (width, height), (250, 250, 252))
    draw = ImageDraw.Draw(img)
    header_h = max(64, height // 9)
    head_font = _load_font(max(24, width // 30))
    body_font = _load_font(max(22, width // 28))
    sub_font = _load_font(max(18, width // 44))
    draw.rounded_rectangle([0, 0, width - 1, header_h], radius=20, fill=(37, 99, 235))
    draw.text((width // 2, header_h // 2), "CourseGenie · 示意图占位",
              font=head_font, fill=(255, 255, 255), anchor="mm")
    text_x = max(24, width // 10)
    max_w = width - text_x * 2
    lines = _wrap_by_width(prompt or "示意图", draw, body_font, max_w)
    line_h = int(body_font.size * 1.7)
    block_h = len(lines) * line_h + 90
    y = header_h + max(24, (height - header_h - block_h) // 2)
    for line in lines:
        draw.text((text_x, y), line, font=body_font, fill=(51, 65, 85))
        y += line_h
    y += 10
    draw.text((text_x, y), "占位图：接入真实图像模型后自动替换为正式插图",
              font=sub_font, fill=(100, 116, 139))
    settings = get_settings()
    out_dir = settings.storage_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{uuid.uuid4().hex}.png"
    img.save(out)
    return out


def _overlay_text(file_path: Path, text: str, *, overlay_pos: str = "top") -> Path:
    """在生成的无字底图上叠加标题文字（半透明衬底 + 中文字体，保证不错字）。

    直接在原文件上保存 PNG；返回同一路径。
    """
    from PIL import Image

    with Image.open(file_path) as src:
        img = src.convert("RGB")
    width, height = img.size
    font = _load_font(max(26, min(width, height) // 22))
    probe = ImageDraw.Draw(img)
    max_w = int(width * 0.8)
    lines = _wrap_by_width(text, probe, font, max_w, limit=3)
    pad_x = int(width * 0.06)
    pad_y = int(height * 0.03)
    line_h = int(font.size * 1.55)
    band_w = min(width - pad_x * 2, int(width * 0.9))
    band_h = pad_y * 2 + line_h * len(lines)
    x0 = (width - band_w) // 2
    margin = max(10, int(height * 0.03))
    if overlay_pos == "bottom":
        y0 = max(0, height - band_h - margin)
    elif overlay_pos == "center":
        y0 = max(0, (height - band_h) // 2)
    else:
        y0 = margin

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    band = ImageDraw.Draw(overlay)
    band.rounded_rectangle([x0, y0, x0 + band_w - 1, y0 + band_h - 1], radius=18,
                           fill=(255, 255, 255, 212))
    final = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(final)
    y = y0 + pad_y
    for line in lines:
        draw.text((width // 2, y), line, font=font, fill=(23, 37, 84), anchor="ma")
        y += line_h
    final.save(file_path, format="PNG")
    return file_path


def _save(b64: str, width: int, height: int) -> Path:
    settings = get_settings()
    out_dir = settings.storage_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{uuid.uuid4().hex}.png"
    out.write_bytes(base64.b64decode(b64))
    return out


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for chunk in text.split():
        if len(chunk) > width:
            lines.extend(chunk[i : i + width] for i in range(0, len(chunk), width))
        else:
            lines.append(chunk)
    return lines[:4]
