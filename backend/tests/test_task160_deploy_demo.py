"""任务 160：部署脚本的两处 restart 必须带 sudo，且生成期自检能拦住漏网命令。"""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "ops" / "deploy_demo.py"

spec = spec_from_file_location("deploy_demo_under_test", SCRIPT)
assert spec and spec.loader
deploy = module_from_spec(spec)
spec.loader.exec_module(deploy)


def _args(target: str) -> SimpleNamespace:
    return SimpleNamespace(target=target, remote_root="/opt/coursegenie",
                           service="coursegenie", port=47831, user="ubuntu")


@pytest.mark.parametrize("target", ["backend", "frontend"])
def test_plan_marks_every_systemctl_command_with_sudo(target):
    steps = deploy.plan(target, _args(target))
    systemctl_steps = [cmd for cmd in steps if "systemctl" in cmd]
    assert systemctl_steps, f"{target} 计划里没有 systemctl 步骤"
    assert all("sudo" in cmd for cmd in systemctl_steps)
    assert all("sudo systemctl restart" in cmd for cmd in systemctl_steps)


def test_assert_safe_steps_rejects_systemctl_without_sudo():
    deploy.assert_safe_steps(["sudo systemctl restart coursegenie", "sleep 1"])
    with pytest.raises(deploy.UnsafeCommandError):
        deploy.assert_safe_steps(["sudo systemctl restart coursegenie",
                                  "systemctl restart coursegenie"])


@pytest.mark.parametrize("target", ["backend", "frontend"])
def test_dry_run_prints_sudo_for_every_systemctl(target, tmp_path, monkeypatch, capsys):
    key = tmp_path / "id_ed25519"
    key.write_text("dummy", encoding="utf-8")
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(b"x")
    monkeypatch.setattr(deploy, "_tar_backend", lambda _work: archive)
    monkeypatch.setattr(deploy, "_tar_frontend", lambda _work: archive)

    code = deploy.main(["--target", target, "--host", "192.0.2.10", "--key", str(key)])
    out = capsys.readouterr().out
    assert code == 0
    systemctl_lines = [line for line in out.splitlines() if "systemctl" in line]
    assert systemctl_lines, out
    assert all("sudo" in line for line in systemctl_lines), systemctl_lines
    assert "演练模式" in out


def test_main_refuses_an_unsafe_generated_plan(tmp_path, monkeypatch):
    key = tmp_path / "id_ed25519"
    key.write_text("dummy", encoding="utf-8")
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(b"x")
    monkeypatch.setattr(deploy, "_tar_backend", lambda _work: archive)
    monkeypatch.setattr(deploy, "plan", lambda *_args: ["systemctl restart coursegenie"])
    with pytest.raises(deploy.UnsafeCommandError):
        deploy.main(["--target", "backend", "--host", "192.0.2.10", "--key", str(key)])


def test_failure_message_points_to_manual_sudo_restart():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "若此步失败，请在服务器上手动执行 sudo systemctl restart" in source
