"""交付包自带的「双击核对文件完整性」脚本（任务 132）。

交付包里本来就有 ``manifest.json``（每个文件的 ``path`` / ``bytes`` / ``sha1``），
但客户要自己核对就得敲 ``certutil -hashfile ... SHA1`` 一条条比 —— 对老师 / 知识付费作者 /
个人客户等于没有（用户 2026-10-05 原话：「客人不可能去敲命令行」）。本模块生成包内那份
**双击就能跑**的 ``核对文件完整性.bat``：只读、不联网、不装依赖、不要管理员，几秒给出
「全部一致 / 有 N 个对不上」。**0 LLM**。

三条硬约束（都是实测踩出来的）：

1. **中文控制台不能崩**（本项目崩过 3 次的老病：默认 cp936/GBK 打印 ``⇒`` 直接
   ``UnicodeEncodeError``）。做法：``.bat`` 先 ``chcp 65001`` 再交给 PowerShell；而且
   **cmd 解析的那半边全是 ASCII** —— 中文只出现在 ``#===PS===`` **之后**那半边
   （PowerShell 用 ``[Text.Encoding]::UTF8`` 读文件取用），cmd 从来不解析它。
2. **不许依赖模块化 cmdlet**：2026-10-05 在本机实测踩到 —— 从 **cmd / 资源管理器 / Python**
   这些「非 PowerShell 父进程」启动时，PSModulePath 里 PowerShell 7 的路径在前，
   Windows PowerShell 5.1 会加载 **PS7 的 ``Microsoft.PowerShell.Utility`` 模块（7.0.0.0）**，
   里面**没有 ``Get-FileHash``**（``-Command "Get-Command Get-FileHash"`` 直接
   ``CommandNotFoundException``；同一台机器从 PowerShell 里启动却有）。
   ⇒ 核对逻辑一律走 .NET（``[IO.File]`` / ``[System.Security.Cryptography.SHA1]`` /
   ``[Console]``），不碰 ``Get-FileHash`` / ``Test-Path`` / ``Join-Path`` 这类 cmdlet；
   唯一保留的 cmdlet 是 ``ConvertFrom-Json``（两边环境都在，且失败会被 catch 成人话）。
3. **它自己也要被钉住**：``tests/test_delivery_integrity_bat.py`` 真跑这个 .bat ——
   改坏一个文件 / 删掉一个文件必须报出来（把它变异成「永远说一致」会红）。

任务 134：客户拿这个包当 Obsidian 仓库打开过之后，Obsidian 会自己生成 ``.obsidian/`` 配置，
包内文件数就比清单多 —— 原来那句「包内 N 个文件」会与实际不符（实测：磁盘 202 个仍说 200 个，
**退出码 0 与「全部一致」的判定本来就是对的**，只是说法不准）。现在**包内总数现数**、
**清单口径单列**，多出来的文件**如实报一行**且**不算错误**（不参与对账、不影响退出码）。

任务 141：多出来那行的**归因也删掉了** —— 原文写死「通常是 Obsidian 自己生成的 .obsidian/
配置」，而 141 在交付包里抓到的反例是 5 个 ``.pdf.textfix`` 临时文件（归因说反了）。
现在只说「是包里多出来的文件、不在核对范围内」，**退出码语义一个字节没动**（多出来仍不算错）。
"""
from __future__ import annotations

#: 包内文件名（中文名，非技术客户要敢点）。
BAT_NAME = "核对文件完整性.bat"
#: cmd 与 PowerShell 的分界：cmd 只执行它**之前**的行并 ``exit /b``；PowerShell 从它**之后**读起。
PS_MARKER = "#===PS==="

_TEMPLATE = """@echo off
rem ============================================================
rem  CourseGenie delivery package - file integrity check
rem  Double-click this file. Read-only: no network, no admin,
rem  no install, and it writes nothing.
rem  It needs manifest.json in the SAME folder as this file.
rem  Windows 10/11 built-in PowerShell does the real work.
rem ============================================================
setlocal
chcp 65001 >nul
set "CG_BAT=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllLines($env:CG_BAT,[Text.Encoding]::UTF8);$i=[Array]::IndexOf($t,'#===PS===');if($i -lt 0){[Console]::WriteLine('PS part not found');exit 4};iex (($t[($i+1)..($t.Length-1)]) -join [char]10)"
set "CG_RC=%ERRORLEVEL%"
echo.
pause
exit /b %CG_RC%
#===PS===
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$root = [IO.Path]::GetDirectoryName($env:CG_BAT)
$manifest = [IO.Path]::Combine($root, 'manifest.json')
if (-not [IO.File]::Exists($manifest)) {
    [Console]::WriteLine('')
    [Console]::WriteLine('❌ 没找到 manifest.json，核对不了。')
    [Console]::WriteLine('')
    [Console]::WriteLine('   请把「核对文件完整性.bat」和「manifest.json」放在同一个文件夹里')
    [Console]::WriteLine('   （也就是交付包解压出来的那一层），再双击一次。')
    [Console]::WriteLine('')
    exit 2
}
try {
    $meta = ConvertFrom-Json ([IO.File]::ReadAllText($manifest, [Text.Encoding]::UTF8))
    $rows = @($meta.files)
} catch {
    [Console]::WriteLine('')
    [Console]::WriteLine('❌ manifest.json 读不出来（可能它自己就损坏了）：' + $_.Exception.Message)
    [Console]::WriteLine('')
    exit 2
}
$sha1 = [System.Security.Cryptography.SHA1]::Create()
$bad = @()
$total = 0
foreach ($row in $rows) {
    $total = $total + 1
    $rel = [string]$row.path
    $full = [IO.Path]::Combine($root, ($rel -replace '/', [IO.Path]::DirectorySeparatorChar))
    if (-not [IO.File]::Exists($full)) {
        $bad += ($rel + '　文件缺失')
        continue
    }
    $size = [IO.FileInfo]::new($full).Length
    if ($size -ne [int64]$row.bytes) {
        $bad += ($rel + '　字节数不符（应为 ' + $row.bytes + '，实际 ' + $size + '）')
        continue
    }
    $stream = [IO.File]::OpenRead($full)
    try { $actual = (($sha1.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $stream.Dispose() }
    if ($actual -ne ([string]$row.sha1).ToLower()) {
        $bad += ($rel + '　期望 sha1 ' + $row.sha1 + '，实际 ' + $actual)
    }
}
[Console]::WriteLine('')
if ($bad.Count -eq 0) {
    # 任务 134：两个数**分开说**，且包内总数**现数**（不再假设「包内 = 清单 + manifest.json」）：
    # 客户拿它当 Obsidian 仓库打开过之后，Obsidian 会自己生成 .obsidian/ 配置，磁盘上就多出文件。
    # 多出来的文件**不在清单里、不参与对账、不算错误**（退出码仍是 0），只如实报一行。
    # 任务 141：那一行**只如实报、不猜原因** —— 134 原来写死「通常是 Obsidian 自己生成的
    # .obsidian/ 配置」，而 141 抓到的反例是包里的 5 个 .pdf.textfix 临时文件（归因说反了）。
    $disk = -1
    $extra = @()
    try {
        $sep = [IO.Path]::DirectorySeparatorChar
        $slash = [char]47
        $present = @([IO.Directory]::EnumerateFiles($root, '*', [IO.SearchOption]::AllDirectories))
        $known = @()
        foreach ($row in $rows) { $known += ([string]$row.path).Replace($sep, $slash) }
        $known += 'manifest.json'
        foreach ($f in $present) {
            $extraRel = $f.Substring($root.Length)
            if ($extraRel.StartsWith([string]$sep)) { $extraRel = $extraRel.Substring(1) }
            $extraRel = $extraRel.Replace($sep, $slash)
            if ($known -notcontains $extraRel) { $extra += $extraRel }
        }
        $disk = $present.Count
    } catch { $disk = -1; $extra = @() }
    if ($disk -gt 0) {
        [Console]::WriteLine('✅ 核对完成：包内 ' + $disk + ' 个文件，其中清单列出的 ' + $total + ' 个逐一核对，全部一致 —— 文件没有被改动、也没有损坏。')
    } else {
        [Console]::WriteLine('✅ 核对完成：清单列出的 ' + $total + ' 个文件逐一核对，全部一致 —— 文件没有被改动、也没有损坏。')
    }
    if ($extra.Count -gt 0) {
        [Console]::WriteLine('　另外 ' + $extra.Count + ' 个文件不在清单里（是包里多出来的文件，不在核对范围内；若你不认识它们，把这段拍给制作方）：')
        $show_extra = [Math]::Min(5, $extra.Count)
        for ($e = 0; $e -lt $show_extra; $e++) { [Console]::WriteLine('　　' + $extra[$e]) }
        if ($extra.Count -gt 5) { [Console]::WriteLine('　　…还有 ' + ($extra.Count - 5) + ' 个没列出来。') }
    }
    [Console]::WriteLine('')
    exit 0
}
[Console]::WriteLine('❌ 有 ' + $bad.Count + ' 个文件对不上（下列是前 5 个）：')
$show = [Math]::Min(5, $bad.Count)
for ($k = 0; $k -lt $show; $k++) { [Console]::WriteLine('　' + $bad[$k]) }
if ($bad.Count -gt 5) { [Console]::WriteLine('　…还有 ' + ($bad.Count - 5) + ' 个没列出来。') }
[Console]::WriteLine('')
[Console]::WriteLine('可能原因：传输 / 解压 / 网盘下载过程中损坏。请重新解压一次；仍不对就把这段拍给制作方。')
[Console]::WriteLine('')
exit 1
"""


def bat_text() -> str:
    """包内那份 .bat 的**逐字节**内容：CRLF 换行；cmd 那半边纯 ASCII，中文只在 ``#===PS===`` 之后。"""
    return _TEMPLATE.replace("\r\n", "\n").replace("\n", "\r\n")
