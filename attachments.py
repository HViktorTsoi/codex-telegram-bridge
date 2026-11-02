"""Telegram attachment transport. Never expose token-bearing URLs to Codex."""
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil

import aiohttp
from reply_context import inputs as reply_inputs

MAX_BYTES = 20_000_000
RESERVE_BYTES = 256 * 1024 * 1024
MEDIA = ("document", "voice", "audio", "video", "video_note", "animation", "sticker")


class AttachmentError(Exception):
    """A permanent, user-readable attachment error."""


def payload_from(message):
    keys = ("photo", "caption", "media_group_id", "location", "venue", "contact", "poll") + MEDIA
    return {key: message[key] for key in keys if key in message}


def media_items(payload):
    photos = payload.get("photo") or []
    if photos:
        best = max(photos, key=lambda p: (p.get("width", 0) * p.get("height", 0), p.get("file_size", 0)))
        return [{**best, "kind": "photo", "file_name": "photo.jpg", "mime_type": "image/jpeg"}]
    for kind in MEDIA:
        if payload.get(kind):
            value = payload[kind]
            extensions = {"voice": ".ogg", "audio": ".mp3", "video": ".mp4", "video_note": ".mp4",
                          "animation": ".mp4", "sticker": ".webp"}
            suffix = extensions.get(kind, ".bin")
            if kind == "sticker" and value.get("is_animated"):
                suffix = ".tgs"
            if kind == "sticker" and value.get("is_video"):
                suffix = ".webm"
            return [{**value, "kind": kind, "file_name": value.get("file_name") or kind + suffix}]
    return []


def image_format(path):
    with path.open("rb") as source:
        header = source.read(16)
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    return None


async def download(telegram, item, directory, maximum=MAX_BYTES):
    size = item.get("file_size") or 0
    if size > maximum:
        raise AttachmentError("附件超过20MB下载上限，请压缩或拆分后发送。")
    try:
        metadata = await telegram.call("getFile", file_id=item["file_id"])
    except Exception as error:
        code = getattr(error, "code", None)
        if code in {400, 404}:
            raise AttachmentError("Telegram 无法提供该附件，请确认文件小于20MB后重新发送。") from None
        if code:
            raise ConnectionError(f"Telegram getFile HTTP {code}") from None
        raise
    if (metadata.get("file_size") or 0) > maximum:
        raise AttachmentError("附件超过20MB下载上限，请压缩或拆分后发送。")
    remote = metadata.get("file_path", "")
    parts = PurePosixPath(remote)
    if not remote or parts.is_absolute() or ".." in parts.parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", remote):
        raise AttachmentError("Telegram 返回的附件下载路径无效，请重新发送。")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    suffix = PurePosixPath(str(item.get("file_name", "")).replace("\\", "/")).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        suffix = ".bin"
    digest = hashlib.sha256(item["file_id"].encode()).hexdigest()[:24]
    target = directory / (digest + suffix.lower())
    if target.is_file() and not target.is_symlink() and 0 < target.stat().st_size <= maximum:
        return target
    if shutil.disk_usage(directory).free < RESERVE_BYTES + max(size, metadata.get("file_size") or maximum):
        raise AttachmentError("本机剩余空间不足，暂时无法保存附件。")
    temporary = target.with_suffix(target.suffix + ".part")
    total = 0
    try:
        # The path is from getFile; redirects are rejected to protect the bot token.
        async with telegram.session.get(
            f"https://api.telegram.org/file/bot{telegram.token}/{remote}",
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                if response.status in {400, 404}:
                    raise AttachmentError("附件已无法从 Telegram 下载，请重新发送。")
                raise ConnectionError(f"Telegram file download HTTP {response.status}")
            if response.content_length and response.content_length > maximum:
                raise AttachmentError("附件超过20MB下载上限，请压缩或拆分后发送。")
            # Never follow a pre-existing symlink for a temporary file.
            temporary.unlink(missing_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > maximum:
                        raise AttachmentError("附件超过20MB下载上限，请压缩或拆分后发送。")
                    output.write(chunk)
        if total == 0:
            raise AttachmentError("下载的附件为空，请重新发送。")
        temporary.replace(target)
        return target
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ConnectionError(f"Telegram file download: {type(error).__name__}") from None
    except ConnectionError:
        raise
    except OSError:
        raise AttachmentError("本机无法保存该附件，请检查磁盘空间和目录权限。") from None
    finally:
        temporary.unlink(missing_ok=True)


async def inputs_for(rows, telegram, inbox_dir):
    texts, files, images, structured = [], [], [], []
    for row in rows:
        if row["text"].strip():
            texts.append(row["text"])
        payload = json.loads(row["message_json"] or "{}")
        for key in ["location", "venue", "contact", "poll"]:
            if key in payload:
                structured.append({key: payload[key]})
        for item in media_items(payload):
            path = await download(telegram, item, Path(inbox_dir) / str(row["id"]))
            picture = image_format(path)
            if item["kind"] == "photo" and not picture:
                raise AttachmentError("下载的图片格式无效，请重新发送或改用文件发送。")
            if picture:
                images.append({"type": "localImage", "path": str(path.resolve())})
            files.append({"kind": item["kind"], "file_name": item["file_name"], "path": str(path.resolve()),
                          "mime_type": item.get("mime_type"), "size_bytes": path.stat().st_size})
    text = "\n\n".join(texts)
    if files:
        if not text:
            text = "请查看我发来的图片或附件，并先概述内容。"
        text += "\n\nTelegram附件已保存至本机，以下是文件元数据（JSON）：\n" + json.dumps(files, ensure_ascii=False)
        if any(f["kind"] in {"voice", "audio", "video", "video_note", "animation"} for f in files):
            text += "\n音频和视频目前作为文件提供，转接层尚未自动转写或提取视频帧；请先确认可用的读取工具，再分析其内容。"
    if structured:
        text += "\n\nTelegram附加信息（JSON）：\n" + json.dumps(structured, ensure_ascii=False)
    result = [{"type": "text", "text": text or "收到了暂未支持的 Telegram 消息类型。"}]
    references = reply_inputs(rows)
    if references:
        result[0]['text'] += '\n\n' + '\n\n'.join(item['text'] for item in references)
    result.extend(images)
    return result
