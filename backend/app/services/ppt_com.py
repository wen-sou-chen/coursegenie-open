"""PowerPoint COM 导出（独立子进程执行，便于超时强杀与失败显式上报）。

放在单独子进程里跑的原因：COM 调用是同步阻塞且可能卡住（PowerPoint 弹窗、进程
残留），主进程无法安全地“超时中断”in-process COM；子进程可以直接 kill。
用法：
    python -m app.services.ppt_com pdf <src.pptx> <target.pdf>
    python -m app.services.ppt_com png <src.pptx> <out_dir>   # 每页一张 PNG
"""
from __future__ import annotations

import sys
from pathlib import Path

PDF_FORMAT = 32  # PowerPoint ppSaveAsPDF
PNG_WIDTH, PNG_HEIGHT = 2560, 1440   # 与 ppt.PPT_PNG_WIDTH/PPT_PNG_HEIGHT 必须一致（任务 107）


def _open_presentation(app, src: Path):
    return app.Presentations.Open(str(src), ReadOnly=True, Untitled=False, WithWindow=False)


def export_pdf(src: Path, target: Path) -> int:
    """整份课件导成 PDF。"""
    if target.exists():
        target.unlink()
    import win32com.client
    app = win32com.client.Dispatch("PowerPoint.Application")
    presentation = None
    try:
        presentation = _open_presentation(app, src)
        presentation.SaveAs(str(target), PDF_FORMAT)
    finally:
        if presentation is not None:
            presentation.Close()
        app.Quit()
    if not target.exists():
        print("PowerPoint 未生成 PDF 产物", file=sys.stderr)
        return 5
    return 0


def export_png(src: Path, out_dir: Path) -> int:
    """每页导出一张 PNG（文件名由 PowerPoint 生成，主进程会统一改名）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    import win32com.client
    app = win32com.client.Dispatch("PowerPoint.Application")
    presentation = None
    try:
        presentation = _open_presentation(app, src)
        presentation.Export(str(out_dir), "PNG", PNG_WIDTH, PNG_HEIGHT)
    finally:
        if presentation is not None:
            presentation.Close()
        app.Quit()
    if not any(p.suffix.lower() == ".png" for p in out_dir.iterdir()):
        print("PowerPoint 未生成 PNG 产物", file=sys.stderr)
        return 5
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("用法：python -m app.services.ppt_com pdf|png <src.pptx> <out>", file=sys.stderr)
        return 2
    mode, src, out = argv[0], Path(argv[1]), Path(argv[2])
    if mode not in ("pdf", "png"):
        print(f"未知模式：{mode}", file=sys.stderr)
        return 2
    if not src.exists():
        print(f"源文件不存在：{src}", file=sys.stderr)
        return 3
    try:
        import win32com.client  # noqa: F401 —— 延迟导入：未安装 pywin32 时给出明确错误
    except ImportError as exc:  # pragma: no cover —— 依赖环境
        print(f"未安装 pywin32，无法调用 PowerPoint COM：{exc}", file=sys.stderr)
        return 4
    return export_pdf(src, out) if mode == "pdf" else export_png(src, out)


if __name__ == "__main__":  # pragma: no cover —— 子进程入口
    sys.exit(main(sys.argv[1:]))
