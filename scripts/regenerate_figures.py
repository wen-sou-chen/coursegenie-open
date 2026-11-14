# -*- coding: utf-8 -*-
"""任务 124：存量超限图的「重跑图生成」（0 LLM、默认 dry-run、幂等）；任务 125 加安全闸；
任务 127 加 ``--migrate-colors``（存量图配色迁移 —— 把图内旧写死色板重映射到主题派生色）。

**为什么需要它**：任务 120 的单图容量上限 / 超限拆图只挂在**生成侧**（LLM 出图那条链路），
而「重建课件」只复用库里既有的图元素、不重新生成 ⇒ 口径改了、存量图不会自己变：
``验收-120-字号四档实测`` 里 17 张超限图**一张没拆**，「页数没涨」是「没拆」的结果，不能算成绩。

**做什么**：按当前口径（``figure_png.over_capacity``：图形元素 > 6 **或** 文本元素 > 20，
任务 125 起两条都生效）把库里的图逐个复测，把**超限且手里还有原始结构化 data** 的那些就地
重跑一遍生成链路（本条原地更新、超限的其余张另建条目，理由记进 ``meta.layout_notes``）；
概念图从任务 124 起才落 ``body.data`` ⇒ 更早的存量概念图**重跑不了**（只能重新生成），
拆不动的（单节点 / 左右对比 / 柱状·折线·饼图·表格）保持原样 —— 都由报告如实交代，不静默。

**安全闸（任务 125）**：``--apply`` 必须把目标三件套 ``--db / --storage / --vault`` **全部
显式写在命令行上**，缺一个就拒绝并退出（连库都不连）；不带 ``--apply`` 时一律 dry-run、只读
不写。⇒ **不带显式参数时本脚本绝不写真实机器**。

**什么时候跑**：图生成侧口径改过之后（字号档位 / 白边 / 单图容量上限 / 拆图分组），
以及每次重建课件前想确认「库里没有超限图」时。先 dry-run，看到「可重跑 > 0」再 ``--apply``。

**``--migrate-colors``（任务 127）**：把「图内取色不是本主题派生」的存量图（任务 124 之前
生成、没有 ``body.data`` ⇒ 重跑不了的那批）就地重映射到目标主题 —— 同色相 + 同明度档，
几何 / 文本 / 字号一个字节不动，并同步重写 SVG 预览；**不反推 data、不重建内容、不动讲义正文**。
先 dry-run 看「待迁移几张」，再 ``--apply``。

用法（临时五件套，别指向真库 / 真 storage）::

    $env:DATABASE_URL="sqlite:///C:/tmp/cg125/coursegenie.db"
    $env:STORAGE_DIR="C:/tmp/cg125/storage"
    $env:EXPORT_DIR="C:/tmp/cg125/exports"
    $env:PPT_WORK_DIR="C:/tmp/cg125/ppt"
    $env:OBSIDIAN_VAULT_ROOT="C:/tmp/cg125/vault"
    cd backend
    .venv\\Scripts\\python.exe ..\\scripts\\regenerate_figures.py --course <course_id>   # 只报不写
    .venv\\Scripts\\python.exe ..\\scripts\\regenerate_figures.py --course <course_id> `
        --db sqlite:///C:/tmp/cg125/coursegenie.db --storage C:/tmp/cg125/storage `
        --vault C:/tmp/cg125/vault --apply                                              # 落库
    .venv\\Scripts\\python.exe ..\\scripts\\regenerate_figures.py --course <course_id> `
        --migrate-colors --theme handdrawn                                                # 配色迁移（只报）
    .venv\\Scripts\\python.exe ..\\scripts\\regenerate_figures.py --course <course_id> `
        --migrate-colors --db sqlite:///C:/tmp/cg127/coursegenie.db `
        --storage C:/tmp/cg127/storage --vault C:/tmp/cg127/vault --apply                # 配色迁移（落库）

``--apply`` 之后回读复测：报告里给出「重跑后仍超限」的张数与其理由（无原始 data / 拆不动），
不达标不静默；本脚本**只动图条目**（body / svg_preview / meta.layout_notes / 拆图后的标题），
不动其它脚本与交付逻辑。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

#: 本项目统一口径（任务 99）：**Windows 默认（GBK）控制台**下也要能跑完 —— 报告里带中文与
#: 箭头，未包装时会在打印中途 UnicodeEncodeError 崩掉（真机上「看着跑了、其实没跑完」）。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

#: 目标三件套（任务 125 的安全闸）：``--apply`` 要三个都显式给；顺序 = 报告里打印的顺序。
_TARGET_ENV = {"db": "DATABASE_URL", "storage": "STORAGE_DIR", "vault": "OBSIDIAN_VAULT_ROOT"}


def _resolve_targets(args) -> list[tuple[str, str, str]]:
    """解析目标三件套：``[(名字, 来源, 生效值)]``。

    优先级：命令行 ``--db/--storage/--vault`` > 同名环境变量 > 程序默认（从仓库根启动时
    就是真机路径）。**只报不写的 dry-run** 也会打印这三行 —— 让人一眼看到「将要读/写哪儿」；
    ``--apply`` 另外要求三个都来自命令行（见 :func:`main`）。
    """
    repo = BACKEND.parent
    defaults = {
        "db": f"sqlite:///{(repo / 'backend' / 'data' / 'coursegenie.db').as_posix()}",
        "storage": str(repo / "backend" / "storage"),
        "vault": "（程序默认：见 .env / Settings，可能是真机知识库）",
    }
    out: list[tuple[str, str, str]] = []
    for name in ("db", "storage", "vault"):
        explicit = str(getattr(args, name) or "")
        env_name = _TARGET_ENV[name]
        if explicit:
            source, value = "命令行", explicit
        elif os.environ.get(env_name):
            source, value = f"环境变量 {env_name}", os.environ[env_name]
        else:
            source, value = "程序默认", defaults[name]
        out.append((name, source, value))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="存量超限图重跑图生成（0 LLM；默认 dry-run 只报不写）")
    parser.add_argument("--course", default="", help="只处理该课程 id（默认全部课程）")
    parser.add_argument("--apply", action="store_true",
                        help="落库（必须同时显式给出 --db/--storage/--vault，否则拒绝）")
    parser.add_argument("--migrate-colors", action="store_true",
                        help="存量图配色迁移（任务 127）：图内旧写死色板 → 主题派生色")
    parser.add_argument("--theme", default="",
                        help="配色迁移的目标主题（id / 中文名；默认用后端 PPT_THEME）")
    parser.add_argument("--json", action="store_true", help="额外把报告打成 JSON")
    parser.add_argument("--db", default="", help="目标数据库（如 sqlite:///C:/tmp/cg/coursegenie.db）")
    parser.add_argument("--storage", default="", help="目标 storage 目录")
    parser.add_argument("--vault", default="", help="目标 Obsidian 知识库根目录（有 Vault 增量写入时）")
    args = parser.parse_args()

    targets = _resolve_targets(args)
    all_explicit = all(source == "命令行" for _name, source, _value in targets)
    print(f"模式：{'--apply 落库' if args.apply else 'dry-run 只报不写'}（目标三件套"
          f"{'全部来自命令行' if all_explicit else '有来自环境变量 / 程序默认的'}）")
    for name, source, value in targets:
        print(f"  目标 {name:<7} = {value}    [{source}]")

    if args.apply:
        missing = [f"--{name}" for name, source, _value in targets if source != "命令行"]
        if missing:
            print(f"\n⛔ 拒绝写入：--apply 要求目标三件套**全部显式写在命令行上**（缺 {'、'.join(missing)}）。",
                  file=sys.stderr)
            print("   这是任务 125 的安全闸：不带显式参数时脚本绝不写真实机器"
                  "（程序默认就是真库 / 真 storage / 真知识库）。", file=sys.stderr)
            print("   临时环境怎么跑（五件套，别指向真库）：", file=sys.stderr)
            print("     cd backend; .venv\\Scripts\\python.exe ..\\scripts\\regenerate_figures.py `",
                  file=sys.stderr)
            print("         --db sqlite:///C:/tmp/cg125/coursegenie.db "
                  "--storage C:/tmp/cg125/storage --vault C:/tmp/cg125/vault --apply", file=sys.stderr)
            return 2
        for name, _source, value in targets:
            os.environ[_TARGET_ENV[name]] = value

    from app.db.session import SessionLocal
    from app.services import figure_regen

    state_text = {
        figure_regen.OK: "口径内",
        figure_regen.NO_ELEMENTS: "没有可重绘元素",
        figure_regen.NO_SOURCE: "无原始 data（重跑不了）",
        figure_regen.NOT_SPLITTABLE: "拆不动",
        figure_regen.READY: "可重跑",
        figure_regen.REFRESHED: "已重跑",
    }

    db = SessionLocal()
    try:
        if args.migrate_colors:
            palette = figure_regen.migrate_figure_colors(
                db, args.course, theme=args.theme or None, apply=args.apply)
            tag = "迁移后" if args.apply else "dry-run"
            print(f"{tag}：图条目 {palette['figures']} · 目标主题 {palette['theme']} · "
                  f"待迁移 {palette['stale']} · 已迁移 {palette['migrated']} · "
                  f"无元素 {palette['no_elements']} · 非主题色值 前 {palette['unknown_before']} "
                  f"→ 后 {palette['unknown_after']}")
            _print_palette_rows(palette["rows"])
            if args.json:
                print(json.dumps({k: v for k, v in palette.items() if k != "rows"},
                                 ensure_ascii=False, indent=1))
            if not args.apply and palette["stale"]:
                print(f"\n有 {palette['stale']} 张图的图内取色不是本主题派生："
                      f"加 --apply 且显式给出 --db/--storage/--vault 才落库")
            return 0
        report = figure_regen.refresh_over_capacity_figures(
            db, args.course, apply=args.apply)
        print(_summary_line("重跑前" if args.apply else "dry-run", report))
        _print_rows(report["rows"], state_text)
        after = report.get("after")
        if after is not None:
            print(_summary_line("重跑后", after))
            print(f"  （重跑后仍超限 {after['over']} 张：无原始 data {after['no_source']}、"
                  f"拆不动 {after['not_splittable']}）")
        if args.json:
            print(json.dumps({k: v for k, v in report.items() if k != "rows"},
                             ensure_ascii=False, indent=1))
        if not args.apply and report["refreshable"]:
            print(f"\n有 {report['refreshable']} 张超限图可以重跑："
                  f"加 --apply 且显式给出 --db/--storage/--vault 才落库")
        return 0
    finally:
        db.close()


def _summary_line(tag: str, report: dict) -> str:
    return (f"{tag}：图条目 {report['figures']} · 超限 {report['over']} "
            f"（超文本上限 {report['over_text']} · 超图形上限 {report['over_shape']}）· "
            f"可重跑 {report['refreshable']} · 已重跑 {report['refreshed']} · "
            f"新增张数 {report['new_parts']} · 无原始 data {report['no_source']} · "
            f"拆不动 {report['not_splittable']} · 无元素 {report['no_elements']}")


def _print_rows(rows: list[dict], state_text: dict) -> None:
    for row in rows:
        if not row["over"]:
            continue
        reason = f"：{row['reason']}" if row["reason"] else ""
        limit = "+".join(t for t, hit in (("文本", row.get("over_text")),
                                          ("图形", row.get("over_shape"))) if hit)
        print(f"  [{state_text.get(row['state'], row['state'])}] {row['kind']:<14} "
              f"{row['layout']:<10} 文本 {row['texts']:>2} 图形 {row.get('shapes', 0):>2} "
              f"超{limit}上限 张数 {row['parts']} {row['title'][:28]}{reason}")


def _print_palette_rows(rows: list[dict]) -> None:
    """配色迁移的逐条明细：只列「迁移前后仍有非主题色」的那些（含具体色值）。"""
    for row in rows:
        if not row["unknown_before"] and not row["unknown_after"]:
            continue
        before = "、".join(row["unknown_before"][:6])
        after = "、".join(row["unknown_after"][:6])
        print(f"  [{row['state']}] {row['kind']:<14} 元素 {row['elements']:>3} "
              f"非主题色 {len(row['unknown_before'])} → {len(row['unknown_after'])} "
              f"{row['title'][:26]}")
        if before:
            print(f"      前：{before}")
        if after:
            print(f"      后：{after}")


if __name__ == "__main__":
    raise SystemExit(main())
