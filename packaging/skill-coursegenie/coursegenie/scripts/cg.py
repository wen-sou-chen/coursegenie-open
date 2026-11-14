#!/usr/bin/env python3
"""CourseGenie 技能脚本：用标准库直接调用课程生成服务（网页版同一套 REST 接口）。

不依赖第三方包；不保存任何凭据。默认连托管演示服务，可用 --base 或
COURSEGENIE_URL 指向自建后端。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_BASE = "https://demo.example.com"
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


class CgError(RuntimeError):
    """面向用户的、可读的错误。"""


def _is_loopback(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


class Api:
    def __init__(self, base: str, api_key: str = "", timeout: float = 120.0) -> None:
        base = (base or DEFAULT_BASE).strip().rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        self.base = base
        self.api_key = api_key.strip()
        self.timeout = timeout
        # 本机/隧道地址必须绕过系统代理（Windows 代理会把它当外网请求，回 502）。
        handlers = [urllib.request.ProxyHandler({})] if _is_loopback(base) else []
        self._opener = urllib.request.build_opener(*handlers)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base}/api{path}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "CourseGenie-Skill/1.0 (+https://demo.example.com)",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: bytes | None = None,
        content_type: str = "",
        timeout: float | None = None,
        raw: bool = False,
    ) -> Any:
        url = self._url(path)
        if params:
            clean = {key: value for key, value in params.items() if value is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        headers = self._headers()
        body = data
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                payload = response.read()
                if raw:
                    return payload
                return json.loads(payload.decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("detail") or parsed.get("error") or detail
            except json.JSONDecodeError:
                pass
            if exc.code == 401:
                raise CgError("后端要求鉴权：请让服务方提供 API Key（COURSEGENIE_API_KEY）。") from None
            raise CgError(f"课程生成服务返回 HTTP {exc.code}：{str(detail)[:500]}") from None
        except urllib.error.URLError as exc:
            raise CgError(f"连不上课程生成服务（{self.base}）：{exc.reason}") from None
        except (ConnectionResetError, TimeoutError, OSError) as exc:
            raise CgError(f"与课程生成服务的连接中断（{self.base}）：{exc}") from None


def _multipart(fields: dict[str, str], filename: str, content: bytes, mime: str) -> tuple[bytes, str]:
    boundary = f"----cg{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    try:
        safe_name = filename.encode("ascii").decode("ascii")
    except UnicodeEncodeError:
        safe_name = "upload" + Path(filename).suffix
    chunks.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        + content
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _poll_job(api: Api, job_id: str, timeout: float) -> Any:
    deadline = time.time() + timeout
    last: Any = {}
    while time.time() < deadline:
        last = api.request("GET", f"/content/jobs/{job_id}", timeout=60)
        status = str((last or {}).get("status") or "")
        if status in ("success", "failed", "cancelled"):
            return last
        time.sleep(2)
    return last


def _safe_extract(archive: Path, target: Path) -> int:
    target_root = target.resolve()
    written = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/") or not name:
                continue
            destination = (target / name).resolve()
            if not str(destination).startswith(str(target_root) + os.sep):
                raise CgError(f"笔记包里出现越界路径，已拒绝解压：{name}")
            if info.file_size > MAX_DOWNLOAD_BYTES:
                raise CgError(f"笔记包里的单个文件过大，已拒绝解压：{name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, destination.open("wb") as sink:
                sink.write(source.read())
            written += 1
    return written


def _cmd_health(api: Api, _args: argparse.Namespace) -> Any:
    return api.request("GET", "/health", timeout=30)


def _cmd_create(api: Api, args: argparse.Namespace) -> Any:
    return api.request(
        "POST",
        "/course/",
        json_body={
            "title": args.title,
            "subject": args.subject,
            "audience": args.audience,
            "duration_minutes": args.minutes,
        },
    )


def _cmd_upload(api: Api, args: argparse.Namespace) -> Any:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise CgError(f"找不到文件：{path}")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body, content_type = _multipart(
        {"course_id": args.course_id, "title": args.title or path.stem},
        path.name,
        path.read_bytes(),
        mime,
    )
    return api.request("POST", "/material/upload", data=body, content_type=content_type, timeout=600)


def _cmd_outline(api: Api, args: argparse.Namespace) -> Any:
    course = api.request("GET", f"/course/{args.course_id}")
    goal = (args.goal or "").strip() or f"讲清《{course.get('title') or '本课程'}》的核心内容"
    return api.request(
        "POST",
        "/plan/generate",
        json_body={
            "course_id": args.course_id,
            "goal": goal,
            "audience": (args.audience or course.get("audience") or "").strip(),
            "num_chapters": args.chapters,
            "style": args.style,
        },
        timeout=args.wait,
    )


def _cmd_approve(api: Api, args: argparse.Namespace) -> Any:
    return api.request(
        "POST",
        "/plan/approve",
        json_body={"course_id": args.course_id, "approved": not args.reject},
    )


def _node_ids(plan: Any) -> list[dict[str, Any]]:
    if isinstance(plan, dict) and isinstance(plan.get("nodes"), list):
        nodes = [node for node in plan["nodes"] if isinstance(node, dict)]
        points = [
            node for node in nodes
            if str(node.get("node_type") or node.get("type") or "") == "point"
        ]
        return points or nodes
    nodes: list[dict[str, Any]] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("node_type") == "point" or item.get("type") == "point":
                nodes.append(item)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(plan)
    return nodes


def _cmd_sample(api: Api, args: argparse.Namespace) -> Any:
    plan = api.request("GET", f"/plan/{args.course_id}")
    points = _node_ids(plan)
    if not points:
        raise CgError("大纲里没有可生成的知识点：先生成大纲并确认。")
    if not 1 <= args.index <= len(points):
        raise CgError(f"--index 超出范围：这门课共 {len(points)} 个知识点。")
    node_id = str(points[args.index - 1].get("id") or "")
    if not node_id:
        raise CgError("大纲节点缺少 id，无法生成样课。")
    if args.figure_style:
        api.request(
            "POST",
            "/lecture/style-anchor",
            json_body={"course_id": args.course_id, "tone": args.tone, "figure_style": args.figure_style},
        )
    queued = api.request(
        "POST",
        "/lecture/generate",
        json_body={
            "course_id": args.course_id,
            "node_id": node_id,
            "tone": args.tone,
            "allow_overwrite_locked": False,
        },
    )
    job_id = str((queued or {}).get("job_id") or "")
    if not job_id:
        raise CgError("后端没有返回 job_id。")
    job = _poll_job(api, job_id, args.wait)
    return {"course_id": args.course_id, "node_id": node_id, "job": job}


def _cmd_all(api: Api, args: argparse.Namespace) -> Any:
    return api.request(
        "POST",
        "/lecture/generate-all",
        json_body={
            "course_id": args.course_id,
            "tone": args.tone,
            "force": False,
            "ignore_style_anchor": False,
            "allow_anchor_overwrite": bool(args.sample_approved),
        },
    )


def _cmd_concepts(api: Api, args: argparse.Namespace) -> Any:
    return api.request("POST", f"/concept/course/{args.course_id}/generate")


def _cmd_status(api: Api, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {}
    for key, path in (
        ("course", f"/course/{args.course_id}"),
        ("plan", f"/plan/{args.course_id}"),
        ("content", f"/content/{args.course_id}"),
    ):
        try:
            payload[key] = api.request("GET", path)
        except CgError as exc:
            payload[key] = {"error": str(exc)}
    if args.job_id:
        payload["job"] = api.request("GET", f"/content/jobs/{args.job_id}")
    return payload


def _cmd_export(api: Api, args: argparse.Namespace) -> Any:
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        raise CgError(f"目标目录不存在：{vault}（请先创建，或换一个已存在的目录）")
    packed = api.request(
        "POST",
        "/publish/export-notes-zip",
        json_body={"course_id": args.course_id, "scope": "all"},
        timeout=args.wait,
    )
    url = str((packed or {}).get("url") or "")
    if not url:
        raise CgError("后端没有返回笔记包地址。")
    archive_bytes = api.request("GET", url.replace("/api", "", 1) if url.startswith("/api/") else url, raw=True, timeout=args.wait)
    course = api.request("GET", f"/course/{args.course_id}")
    title = str(course.get("title") or args.course_id)
    safe_title = "".join(ch for ch in title if ch not in '\\/:*?"<>|').strip() or args.course_id
    target = vault / safe_title
    target.mkdir(parents=True, exist_ok=True)
    tmp = vault / f".{safe_title}.notes.zip"
    tmp.write_bytes(archive_bytes)
    try:
        written = _safe_extract(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "local_dir": str(target), "files_written": written, "zip": (packed or {}).get("file")}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cg.py", description="CourseGenie 技能命令行")
    parser.add_argument("--base", default=os.environ.get("COURSEGENIE_URL", DEFAULT_BASE))
    parser.add_argument("--api-key", default=os.environ.get("COURSEGENIE_API_KEY", ""))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")

    create = sub.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--audience", default="")
    create.add_argument("--subject", default="")
    create.add_argument("--minutes", type=int, default=60)

    upload = sub.add_parser("upload")
    upload.add_argument("--course-id", required=True)
    upload.add_argument("--file", required=True)
    upload.add_argument("--title", default="")

    outline = sub.add_parser("outline")
    outline.add_argument("--course-id", required=True)
    outline.add_argument("--goal", default="")
    outline.add_argument("--audience", default="")
    outline.add_argument("--chapters", type=int, default=4)
    outline.add_argument("--style", default="正式")
    outline.add_argument("--wait", type=float, default=600.0)

    approve = sub.add_parser("approve")
    approve.add_argument("--course-id", required=True)
    approve.add_argument("--reject", action="store_true")

    sample = sub.add_parser("sample")
    sample.add_argument("--course-id", required=True)
    sample.add_argument("--index", type=int, default=1)
    sample.add_argument("--tone", default="正式")
    sample.add_argument("--figure-style", default="")
    sample.add_argument("--wait", type=float, default=600.0)

    all_cmd = sub.add_parser("all")
    all_cmd.add_argument("--course-id", required=True)
    all_cmd.add_argument("--tone", default="正式")
    all_cmd.add_argument("--sample-approved", action="store_true")

    concepts = sub.add_parser("concepts")
    concepts.add_argument("--course-id", required=True)

    status = sub.add_parser("status")
    status.add_argument("--course-id", required=True)
    status.add_argument("--job-id", default="")

    export = sub.add_parser("export")
    export.add_argument("--course-id", required=True)
    export.add_argument("--vault", required=True)
    export.add_argument("--wait", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _build_parser().parse_args(argv)
    api = Api(args.base, args.api_key)
    handlers = {
        "health": _cmd_health,
        "create": _cmd_create,
        "upload": _cmd_upload,
        "outline": _cmd_outline,
        "approve": _cmd_approve,
        "sample": _cmd_sample,
        "all": _cmd_all,
        "concepts": _cmd_concepts,
        "status": _cmd_status,
        "export": _cmd_export,
    }
    try:
        result = handlers[args.command](api, args)
    except CgError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
