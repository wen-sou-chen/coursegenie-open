"""把代码/前端产物部署到演示服务器（默认只演练，加 --yes 才真执行）。

为什么要有它
------------
演示机（腾讯轻量）的上云步骤一直散在临时目录的几个脚本里，换台机器就没了；而且「部署前端」
和「部署后端」是两件事，混在一起容易只更新一半（前端新、后端旧，或反过来），最难排查。
这个脚本把两种部署各自写成一条确定的路，并且**默认不执行**。

用法
----
    # 演练：只打印将执行的命令
    py -3 tools/ops/deploy_demo.py --target backend  --host <IP>
    py -3 tools/ops/deploy_demo.py --target frontend --host <IP>

    # 真执行
    py -3 tools/ops/deploy_demo.py --target backend  --host <IP> --yes
    py -3 tools/ops/deploy_demo.py --target frontend --host <IP> --yes

前置
----
- 本机有 `backend/`、`frontend/dist/`（前端要先 `npm run build`）；
- 本机 `pip install paramiko`（或用带 paramiko 的解释器运行）；
- SSH 私钥：`--key` 指定，或默认 `~/.ssh/id_ed25519`；**不接收密码参数**，密码不进命令行历史。

安全约定
--------
- 只读演练是默认行为；写操作必须显式 `--yes`；
- `.env` 在解包前备份、解包后还原（部署后端不会覆盖服务器配置）；
- 不打印任何密钥；不碰服务器上端口 80/443 的其他服务；
- 每一步打印「命令 + 退出码 + 关键输出末尾」，失败立即停。
"""

from __future__ import annotations

import argparse
import os
import posixpath
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(2)


class UnsafeCommandError(ValueError):
    """生成期自检失败：命令含 systemctl 却没有 sudo。"""


def assert_safe_steps(steps: list[str]) -> None:
    """打印 / 执行前逐条检查：含 systemctl 的命令必须显式带 sudo。"""
    for index, cmd in enumerate(steps, 1):
        if "systemctl" in cmd and "sudo" not in cmd:
            raise UnsafeCommandError(f"第 {index} 步含 systemctl 但缺 sudo：{cmd}")


def _tar_backend(work: Path) -> Path:
    out = work / "backend.tar.gz"
    srcs = [REPO / "backend" / "app", REPO / "backend" / "requirements.txt", REPO / "backend" / "alembic.ini"]
    missing = [str(p) for p in srcs if not p.exists()]
    if missing:
        _die(f"缺文件：{missing}")
    with tarfile.open(out, "w:gz") as tar:
        for src in srcs:
            tar.add(src, arcname=str(Path("backend") / src.name))
    return out


def _tar_frontend(work: Path) -> Path:
    dist = REPO / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        _die("frontend/dist 不存在或不完整 —— 先在 frontend/ 跑 npm run build")
    out = work / "dist.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for item in dist.iterdir():
            tar.add(item, arcname=item.name)
    return out


def plan(target: str, args: argparse.Namespace) -> list[str]:
    """要执行的远端命令序列（演练时原样打印，真执行时逐条跑）。"""
    root, svc = args.remote_root, args.service
    if target == "backend":
        return [
            f"cp {root}/backend/.env /tmp/env.keep 2>/dev/null && chmod 600 /tmp/env.keep; echo env-saved",
            f"cd {root} && rm -rf backend.new && mkdir -p backend.new && tar -xzf /tmp/backend.tar.gz -C backend.new",
            f"cd {root} && rm -rf backend && mv backend.new/backend backend && cp /tmp/env.keep backend/.env && chmod 600 backend/.env",
            f"export PATH=\"$HOME/.local/bin:$PATH\"; VIRTUAL_ENV={root}/venv uv pip install "
            f"--python {root}/venv/bin/python -r {root}/backend/requirements.txt "
            f"--index-url https://pypi.tuna.tsinghua.edu.cn/simple "
            f"|| {root}/venv/bin/python -m pip install -q -r {root}/backend/requirements.txt",
            f"sudo systemctl restart {svc} && sleep 6 && systemctl is-active {svc}",
            f"curl -s -m 10 -o /dev/null -w 'health=%{{http_code}}\\n' http://127.0.0.1:{args.port}/api/health",
        ]
    return [
        f"sudo rm -rf {root}/frontend-dist.new && sudo mkdir -p {root}/frontend-dist.new",
        f"sudo tar -xzf /tmp/dist.tar.gz -C {root}/frontend-dist.new",
        f"sudo rm -rf {root}/frontend-dist.prev && sudo mv {root}/frontend-dist {root}/frontend-dist.prev 2>/dev/null; sudo mv {root}/frontend-dist.new {root}/frontend-dist",
        f"sudo chown -R {args.user}:{args.user} {root}/frontend-dist",
        f"sudo systemctl restart {svc} && sleep 6 && systemctl is-active {svc}",
        f"curl -s -m 10 -o /dev/null -w 'root=%{{http_code}}\\n' http://127.0.0.1:{args.port}/",
    ]


def verify_public(url: str) -> None:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=20) as resp:
            body = resp.read(200).decode("utf-8", "replace")
        print(f"✓ 本机访问 {url}/api/health → {resp.status} {body[:120]}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"! 本机访问 {url} 失败（{exc}）—— 多半是本机代理/网络；以服务器本地 curl 结果为准")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="演示服务器部署（默认演练）")
    parser.add_argument("--target", required=True, choices=["backend", "frontend"])
    parser.add_argument("--host", required=True, help="服务器 IP 或域名")
    parser.add_argument("--user", default=os.environ.get("SSH_USER", "ubuntu"))
    parser.add_argument("--key", default=os.environ.get("SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519")))
    parser.add_argument("--remote-root", default="/opt/coursegenie")
    parser.add_argument("--service", default="coursegenie")
    parser.add_argument("--port", type=int, default=47831)
    parser.add_argument("--yes", action="store_true", help="真执行（不加就只演练）")
    args = parser.parse_args(argv)

    if not Path(args.key).is_file():
        _die(f"找不到私钥 {args.key}（用 --key 指定，或设置 SSH_KEY）")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        archive = _tar_backend(work) if args.target == "backend" else _tar_frontend(work)
        size_mb = archive.stat().st_size / 1e6
        remote_archive = f"/tmp/{'backend' if args.target == 'backend' else 'dist'}.tar.gz"
        steps = plan(args.target, args)
        assert_safe_steps(steps)

        print(f"== 目标：{args.target}  →  {args.user}@{args.host}{args.remote_root}（包 {size_mb:.1f} MB）")
        for i, cmd in enumerate(steps, 1):
            print(f"  {i}. {cmd}")

        if not args.yes:
            print("\n（演练模式：什么都没做。确认无误后加 --yes 真执行）")
            return 0

        try:
            import paramiko  # noqa: PLC0415
        except ImportError:
            _die("本机缺 paramiko：pip install paramiko（或改用带它的虚拟环境运行）")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(args.host, username=args.user, pkey=paramiko.Ed25519Key.from_private_key_file(args.key),
                       timeout=20, banner_timeout=30, auth_timeout=20, allow_agent=False, look_for_keys=False)
        try:
            sftp = client.open_sftp()
            sftp.put(str(archive), remote_archive)
            sftp.close()
            print(f"✓ 上传 {remote_archive}")
            for i, cmd in enumerate(steps, 1):
                _, out, err = client.exec_command(cmd, timeout=900)
                code = out.channel.recv_exit_status()
                o = out.read().decode("utf-8", "replace").strip()
                e = err.read().decode("utf-8", "replace").strip()
                print(f"[{i}/{len(steps)}] exit={code} {cmd[:80]}")
                if o:
                    print("   " + "\n   ".join(o.splitlines()[-4:]))
                if code != 0:
                    if e:
                        print("   [stderr] " + e.splitlines()[-1][:300])
                    _die(f"第 {i} 步失败，已停止（服务器现状：前 {i - 1} 步已生效）。"
                         f"若此步失败，请在服务器上手动执行 sudo systemctl restart {args.service}")
        finally:
            client.close()

        verify_public(f"http://{args.host}:{args.port}")
        print("\n✓ 部署完成。别忘了：前端改动要强制刷新（Ctrl+F5）才能看到新的 assets。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
