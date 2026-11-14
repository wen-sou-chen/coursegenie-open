"""上传读取：Content-Length 预检 + 分块读取 + 大小上限（P1-4）。

material/image/ppt 上传共用；超过上限抛 UploadTooLarge，由路由转 413。

任务 102：音视频上限 2GB，**不能先整份读进内存**再判 ⇒ 另给
:func:`read_upload_to_path`：分块**直接落盘**，超限即中断并删掉半个文件。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import UploadFile

READ_CHUNK = 64 * 1024


class UploadTooLarge(ValueError):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"文件超过大小上限（{round(limit_bytes / 1024 / 1024, 1)} MB）")
        self.limit_bytes = limit_bytes


async def read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """分块读取上传文件正文；超过 max_bytes 抛 UploadTooLarge。

    先按 Content-Length 预检，避免大文件先进内存；正文读取同样分块计数，
    超限即中断，不会把整份超大文件读入内存后再拒绝。
    """
    length_header = file.headers.get("content-length")
    if length_header:
        try:
            if int(length_header) > max_bytes:
                raise UploadTooLarge(max_bytes)
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(READ_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise UploadTooLarge(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_upload_to_path(file: UploadFile, dest: Path, max_bytes: int) -> int:
    """分块把上传正文**写到磁盘**，返回字节数；超过 max_bytes 抛 UploadTooLarge。

    先按 Content-Length 预检（省掉大文件的前半段），正文仍逐块计数、超限立刻中断，
    并删掉写了一半的目标文件（音视频上限 2GB，读进内存再拒会把进程撑爆）。
    """
    length_header = file.headers.get("content-length")
    if length_header and length_header.isdigit() and int(length_header) > max_bytes:
        raise UploadTooLarge(max_bytes)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(max_bytes)
                out.write(chunk)
    except UploadTooLarge:
        dest.unlink(missing_ok=True)
        raise
    return size
