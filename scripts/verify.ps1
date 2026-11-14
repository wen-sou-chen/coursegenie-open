<#
.SYNOPSIS
    CourseGenie 一键校验：后端 pytest + 前端 tsc + 前端 build。

.DESCRIPTION
    用法（在仓库任意目录执行都行，脚本自己定位仓库根）：
        powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
        powershell -ExecutionPolicy Bypass -File scripts\verify.ps1 -SkipBuild     # 只跑 pytest + tsc

    任一环节失败 → 汇总打印失败项并以非零码退出（可直接用于提交前自检 / CI）。

    安全：测试期间 DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT / PPT_WORK_DIR / EXPORT_DIR
    五者全部指向本次运行的临时目录（AI 协作区 README 第 7 条，2026-09-17 修正为五件套），
    不会碰真实数据库、storage（含 storage/ppt 与 storage/exports）与 D:\CourseGenie知识库。
#>
[CmdletBinding()]
param(
    [switch]$SkipBuild,      # 跳过 vite build
    [switch]$Backend,        # 只跑后端
    [switch]$Frontend        # 只跑前端
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repo "backend"
$frontendDir = Join-Path $repo "frontend"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "cg_verify_$stamp"

$env:DATABASE_URL = "sqlite:///" + (($tmpDir -replace "\\", "/") + "/verify.db")
$env:STORAGE_DIR = Join-Path $tmpDir "storage"
$env:OBSIDIAN_VAULT_ROOT = Join-Path $tmpDir "vault"
$env:PPT_WORK_DIR = Join-Path $tmpDir "storage\ppt"
$env:EXPORT_DIR = Join-Path $tmpDir "storage\exports"
$env:PYTHONIOENCODING = "utf-8"
$env:LLM_PROVIDER = "mock"
$env:IMAGE_PROVIDER = "mock"
New-Item -ItemType Directory -Force -Path $env:STORAGE_DIR, $env:OBSIDIAN_VAULT_ROOT | Out-Null

function Resolve-Exe([string]$Name) {
    foreach ($candidate in @("$Name.cmd", $Name)) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "找不到可执行文件：$Name"
}

$python = Join-Path $backendDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = Resolve-Exe "python" }

$failures = @()
function Invoke-Step {
    param(
        [string]$Title,
        [string]$WorkDir,
        [string]$File,
        [string[]]$StepArgs
    )
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Push-Location $WorkDir
    try {
        & $File @StepArgs
        $code = $LASTEXITCODE
    }
    catch {
        Write-Host "执行异常：$($_.Exception.Message)" -ForegroundColor Red
        $code = 1
    }
    finally {
        Pop-Location
        $sw.Stop()
    }
    $secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    if ($code -eq 0) {
        Write-Host "[PASS] $Title ($secs s)" -ForegroundColor Green
    }
    else {
        Write-Host "[FAIL] $Title (exit=$code, $secs s)" -ForegroundColor Red
        $script:failures += "$Title (exit=$code)"
    }
}

Write-Host "CourseGenie 一键校验 · 仓库 $repo"
Write-Host "临时目录（DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT）：$tmpDir"

if (-not $Frontend) {
    Invoke-Step -Title "连接器包自检（17 项）" -WorkDir $repo -File (Resolve-Exe "py") -StepArgs @("-3", "tools/ops/connector_check.py")
    Invoke-Step -Title "后端 pytest" -WorkDir $backendDir -File $python -StepArgs @("-m", "pytest", "-q")
}
if (-not $Backend) {
    Invoke-Step -Title "前端 tsc --noEmit" -WorkDir $frontendDir -File (Resolve-Exe "npx") -StepArgs @("tsc", "--noEmit")
    Invoke-Step -Title "前端口径断言（流程图计数）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-content-counts.ts")
    Invoke-Step -Title "前端公式渲染断言（KaTeX）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-math-render.ts")
    Invoke-Step -Title "前端质检现算断言（refresh=true）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-qc-refresh.ts")
    Invoke-Step -Title "前端交付模式断言（对外默认）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-delivery-mode.ts")
    Invoke-Step -Title "前端用量面板形状断言（aux_history 是对象）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-usage-panel-shape.ts")
    Invoke-Step -Title "前端面板加载态断言（48z：加载中不伪装成空态）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-panel-loading.ts")
    Invoke-Step -Title "前端鉴权头断言（48u：登录带 X-API-Key / 演示模式只留分享页）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-auth-header.ts")
    Invoke-Step -Title "前端 antd 上下文断言（48t：App.useApp + favicon）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-antd-context.ts")
    Invoke-Step -Title "前端数据卡去重断言（64：正文已有同数据表格则跳过）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-chart-table-dedup.ts")
    Invoke-Step -Title "前端知识图谱布局断言（68：章 → 知识点树形、不重叠）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-knowledge-graph-layout.ts")
    Invoke-Step -Title "前端步骤条序号断言（68：不再出现「4④ 课程预览」）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-steps-no-duplicate-index.ts")
    Invoke-Step -Title "前端知识图谱可读性断言（71：默认缩放 ≥0.8、字号 ≥10px、弹窗 ≥92vw×78vh）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-knowledge-graph-readability.ts")
    Invoke-Step -Title "前端素材注入提示断言（69：超限提示 + 「已清理 N 行」在界面有落点）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-material-injection-ui.ts")
    Invoke-Step -Title "前端大纲自检断言（70：结论落库常显 + 覆盖扩面 + 语义默认关）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-outline-selfcheck.ts")
    Invoke-Step -Title "前端素材三态文案断言（72：库内保留 / 出库剥离 / 已清理 + 显式补齐入口）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-material-meta-copy.ts")
    Invoke-Step -Title "前端课程预览交互断言（74：筛选回显/清除 + 跳返原位 + 预览抽屉 + 诊断折叠 + 例句摘自本篇）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-preview-ux.ts")
    Invoke-Step -Title "前端预览页按章节聚合断言（113：默认按章节/类型视图可切换 + 章卡内固定顺序 + 章级课件就地折叠 + 两视图条数一致 + 选择记忆 + 8 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-preview-chapter-grouping.ts")
    Invoke-Step -Title "前端预览页跳转断言（114：章卡内课件前置 + 点「课件（N）」先展开再滚 + 落点取展开后的位置 + 已展开不重复开 + 无落点不乱跳 + 变异反证）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-preview-anchor-jump.ts")
    Invoke-Step -Title "前端素材要点质量与呈现断言（75：去装饰/限长 24/案例类取消 + 默认前 8 条 + 展开 + filtered_out_count）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-point-quality.ts")
    Invoke-Step -Title "前端预览点图放大断言（76：概念图/数据图/流程图点开即弹 + 只看概念图自动滚 + 章容器空态收敛）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-figure-zoom.ts")
    Invoke-Step -Title "前端概念图图标口径断言（77：白名单 16 个 + 兜底轮换子集 + 提示词约束 + 版本号 + 标题条不再用 👨‍🍳）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-diagram-emoji.ts")
    Invoke-Step -Title "前端预览页图的入场动效断言（79：淡入上浮 + 遮罩扫出 + 视口一次 + 滚动停止补播 + 同时 ≤8 + 可关）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-figure-entrance.ts")
    Invoke-Step -Title "前端图件口径断言（80：位图配图 / 概念图 / 数据图 + 旧管线按钮与提示不说「不含配图」）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-wording-image-vs-diagram.ts")
    Invoke-Step -Title "前端面板数字与呈现断言（81：字数口径 / 时间本地化 / 大纲自检空态 / 质量报告快照说明）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-panel-metrics-copy.ts")
    Invoke-Step -Title "前端时间口径断言（83：裸 UTC 补时区 + 时间格式化唯一出处 + 后端出口带时区 + 自检入口日志）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-time-format.ts")
    Invoke-Step -Title "前端裸切时间戳断言（84：课件时间走本地时间 + 源码级不许裸切 / 不许手工拼 UTC 后缀）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-bare-time-slice.ts")
    Invoke-Step -Title "前端使用场景断言（85：两侧表逐字一致 + 未选场景不补默认值 + 界面接线与三处提示词）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-scenario.ts")
    Invoke-Step -Title "前端口径可见断言（86：三处生成点写标记 + 笔记 frontmatter + 徽标 + 会变/不会变 + 成本提示）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-scenario-visibility.ts")
    Invoke-Step -Title "前端学员版断言（86：默认教师版 + 去答案复用学案规则 + 0-LLM 护栏 + 分享切换）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-student-view.ts")
    Invoke-Step -Title "前端 frontmatter 键名断言（86b：真 import frontmatter_fields + 键名/值 + return [] 变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-scenario-frontmatter.ts")
    Invoke-Step -Title "前端公开侧载荷断言（89：白名单投影 + 学员版不带历史版本 + 真 import public_meta）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-public-payload.ts")
    Invoke-Step -Title "前端录课包断言（87：句长/闸门常量 + 场景门 + 真跑 Python 的 SRT/逐字稿 + 8 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-recording-kit.ts")
    Invoke-Step -Title "前端 HTML 课件断言（88：单文件/打印 A4 横版/手机不溢出 + 真跑 Python 渲染 + 7 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-html-deck.ts")
    Invoke-Step -Title "前端空答案标签断言（90：答案/解析统一按真值渲染 + 学员版不留空键 + 6 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-share-answer-labels.ts")
    Invoke-Step -Title "前端审核锁断言（91：单篇重跑默认 409 + 覆盖即摘徽标 + 批量跳过语义不动 + 6 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-lecture-lock-overwrite.ts")
    Invoke-Step -Title "前端网页版课件预览断言（94：复用出包同一渲染 + 共用积木按钮 + 真调接口与包内逐字节一致 + 5 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-deck-html-preview.ts")
    Invoke-Step -Title "前端动效入口断言（109：入口与「预览」同级 + 一句话差异常显在抽屉外 + 失败说人话；5 档异常 + 5 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-ppt-html-entry.ts")
    Invoke-Step -Title "前端讲师模式断言（150：三条只读路由 + 共用入口 + 五项交互 + 结构页守卫 + 6 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-presenter-mode.ts")
    Invoke-Step -Title "前端学生版断言（151：两条只读路由 + 共用入口 + 五项交互 + 答案默认隐藏 + 7 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-student-mode.ts")
    Invoke-Step -Title "前端受众与删除断言（158：共享 AudienceField + 候选/示例/说明 + 删除强确认 + 后端按 id 清理）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-task158.ts")
    Invoke-Step -Title "前端预览答案折叠断言（160：纯函数 1 题/2 题/无答案/中间答案 + 默认收起 + 只预览页接线 + 学员版）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-task160.ts")
    Invoke-Step -Title "前端存量概念图图标归一断言（92：渲染器缺省值不再长 👨‍🍳 + 迁移复用同一套白名单 + 默认 dry-run + 幂等 + 4 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-diagram-icon-normalize.ts")
    Invoke-Step -Title "前端风格库角色组断言（101：缺省中性三件套 + 按 course.meta.roles 配置 + 其它 14 条不变 + 非法回落 + 6 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-style-library-roles.ts")
    Invoke-Step -Title "前端音视频转写断言（102：扩展名两侧一致 + 转写异步入队 + [mm:ss] 分段 + meta.asr + 降级不 500；103：中文默认简体引导语 + 非中文不传；104：ASR_MODEL 走 .env（Settings 回落 + 进程环境变量优先）+ 12 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-material-asr.ts")
    Invoke-Step -Title "前端从链接导入断言（118/119：入口 + 顺序写死（先字幕→音频→明确失败） + 依赖探测只查版本不联网、缺了说人话不 500 + 限流 4s·20s 与 429 退避 8→16→32 上限 3 次 + cookie 只留布尔、默认值走 .env⇒进程环境变量⇒PATH 回落链 + 0 LLM；5 条源码变异 + 2 条行为变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-material-link.ts")
    Invoke-Step -Title "前端主题跟随课程断言（145：两侧表逐字一致 + 优先级四档（显式＞本课＞配置＞默认）+ 非法值不静默 + 迁移幂等 + 真调接口 + 6 条变异）" -WorkDir $frontendDir -File (Resolve-Exe "node") -StepArgs @("scripts/check-ppt-course-theme.ts")
    Invoke-Step -Title "档案结构体检（2026-10-24：8 段齐全 / 篇幅 / 占位 / 新鲜度）" -WorkDir $repo -File $python -StepArgs @("tools/ops/profile_build.py", "check")
    Invoke-Step -Title "档案隐私守卫（2026-10-24：profile/ 不含身份证 / 手机号 / 邮箱 / 地址）" -WorkDir $repo -File $python -StepArgs @("tools/ops/profile_guard.py")
    if (-not $SkipBuild) {
        Invoke-Step -Title "前端 vite build" -WorkDir $frontendDir -File (Resolve-Exe "npm") -StepArgs @("run", "build")
    }
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "全部通过 ✅" -ForegroundColor Green
    exit 0
}
Write-Host "失败项：" -ForegroundColor Red
foreach ($f in $failures) { Write-Host "  - $f" -ForegroundColor Red }
exit 1
