"""应用配置：环境变量优先，缺省值保证零配置可跑（SQLite + Mock LLM）。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "CourseGenie API"
    debug: bool = True
    api_prefix: str = "/api"

    # 数据库：缺省 SQLite，可通过 DATABASE_URL 切 PostgreSQL
    database_url: str = "sqlite:///./data/coursegenie.db"

    # 存储
    storage_dir: Path = Path("./storage")
    export_dir: Path = Path("./storage/exports")
    ppt_work_dir: Path = Path("./storage/ppt")
    # PDF 预览（可选增强，默认关闭）：开启且探测到 PowerPoint 时用 COM 导 PDF
    ppt_preview_pdf: bool = False
    # 幻灯片图片（默认开启）：用 PowerPoint COM 把每页导成 PNG，网页端像图库一样直出
    ppt_slide_images: bool = True
    # 每章课件里「每个知识点」的页数（1~3，默认 3）：页数由文本块与图页共同决定。
    # 任务 146b：默认 2 会把一个知识点里的第 3 张图挤进备注（图没了）；默认 3 让
    # 「每张图都有自己的页」先成立，再由 _point_slides 按内容删减。
    ppt_slides_per_point: int = 3
    # PPT 主题：clean 清简教学 / professional 专业培训 / handdrawn 手绘亲和 /
    # modern 现代简约 / navy 深蓝商务（中文名亦可，见 services/ppt_theme.py 的 THEMES）
    ppt_theme: str = "clean"
    # 页脚「课程名 · 章号 章名 · P页码」：false 时整条不画
    ppt_show_footer: bool = True
    # 可选水印（如「试看版·请勿外传」）：留空不画
    ppt_watermark: str = ""
    # 导出时是否附带 `.excalidrawlib` 风格库（任务 24-B：图表库/_风格库.excalidrawlib）
    export_style_library: bool = True
    # 每章课件是否附 1 页课堂提问（参考答案写在演讲备注里）
    ppt_include_questions: bool = True
    # 封面作者 / 机构：留空则只显示日期
    ppt_cover_author: str = ""
# 上传大小上限（MB）：material/image/ppt 上传统一限制，防止无上限读取整文件到内存
    max_upload_mb: int = 50

    # 任务 115：后端托管前端静态产物（frontend/dist）—— 让别人「只起一个 uvicorn」就能用整个应用。
    # ⚠️ 默认必须是**开**：这是**产品口径**（「给别人用」时不该要求对方装 Node、开两个终端），
    # 不是随手写的默认值 —— 任务 116 用 `tests/test_serve_frontend.py::test_serve_frontend_default_is_on`
    # 把它钉住了：谁把它改成 False，那条用例立刻红。
    # 关掉（SERVE_FRONTEND=false）= 改造前那套纯 API 行为（Docker 里前端由 nginx 单独跑的场景）。
    serve_frontend: bool = True
    # 前端 dist 目录：留空则自动找（仓库 frontend/dist、当前工作目录下的 frontend/dist）；
    # 显式设置时以它为准 —— 找不到就按「前端未构建」处理，不再去别处猜。
    frontend_dist_dir: str = ""

    # 认证（MVP 单机默认关闭；开启后所有接口需 X-API-Key）
    # 注意：本项目暂无多用户租户隔离——开启认证只挡住未持 Key 的访问，
    # 任何有效 Key（含 DEV_API_KEY 与任意注册用户 Key）都能访问全部课程。
    auth_enabled: bool = False
    dev_api_key: str = "dev-key-change-me"

    # 任务 48u：只读演示模式（对外穿透/公网演示用）。开启后只有 /api/public/* 与
    # /api/health 的 GET/HEAD/OPTIONS 能通过，其余接口一律 403 —— 即使反代配错，
    # 也不会有人在你的机器上生成内容、翻全库或下载交付包。
    demo_readonly: bool = False

    # 任务 154：单租户演示模式（对外「真接客」用）。开启后：
    # - 不提供账号体系（/auth/register、/auth/login 直接 403），所有人共用同一个匿名演示空间；
    # - 每个 IP 每分钟 N 次生成、滚动 24 小时 M 门课、同时只跑 1 个生成任务；
    # - 课程/素材/导出/课件/演示 vault 在 24 小时后自动清理。
    # ⚠️ 自建实例把 OBSIDIAN_VAULT_ROOT 指向真实笔记库时不要开这个开关。
    demo_only: bool = False
    demo_retention_hours: int = 24
    demo_max_generations_per_minute: int = 6
    demo_max_courses_per_day: int = 5
    demo_max_concurrent_jobs: int = 1
    # 托管在 Cloudflare 隧道后面时，用 CF-Connecting-IP / X-Forwarded-For 区分客户端；
    # 直接暴露到公网且没有可信反代时请设为 false，否则 IP 可被伪造。
    demo_trust_proxy_headers: bool = True

    # LLM：mock 保证无密钥也能跑通全流程
    llm_provider: str = "mock"  # openai | anthropic | mock
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_disable_thinking: bool = False
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-sonnet-latest"
    # OpenRouter：OpenAI 兼容协议，一个 key 路由多模型
    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # 任务 23：批量按章生成的知识点并发数（1~4，默认 1 = 顺序执行）
    llm_concurrency: int = 1
    # LLM 单次请求超时（秒）
    llm_timeout_s: int = 180
    # 模型单价覆盖（元 / 百万 tokens）：JSON，如 {"deepseek-chat": {"input": 2, "output": 3}}
    model_prices: str = ""

    # Obsidian 知识库：教师本地 vault 根目录（默认值，课程可单独覆盖）
    obsidian_vault_root: str | None = None

    # 图像生成：mock 生成占位图
    image_provider: str = "mock"  # openai | stability | openrouter | mock
    image_model: str = "google/gemini-2.5-flash-image"  # image_provider=openrouter 时使用的图像模型
    stability_api_key: str | None = None

    # 语音转写（可选依赖 requirements-asr.txt）：模型名。**进程环境变量 ASR_MODEL 与 `.env` 里的
    # ASR_MODEL 都能覆盖**（与本项目其它配置一致）；small 快但中文易错，medium / large-v3 更准更慢
    asr_model: str = "small"
    # 任务 118：从链接导入（YouTube）用的 cookies.txt 路径（可选）。**只按路径交给 yt-dlp**，
    # 不复制、不进日志、不进接口返回、不写进库；留空就不带 cookie（YouTube 常要求登录态）。
    ytdlp_cookies: str = ""
    # 任务 119：yt-dlp 怎么启动。**进程环境变量与 `.env` 都能覆盖**（与 ASR_MODEL 同款回落链：
    # 进程环境变量优先 ⇒ `.env` ⇒ PATH 上的 yt-dlp 与常见落地位置）—— 以前只读 `os.environ`，
    # 往 `.env` 里写这两项**完全不生效**（pydantic 只把 `.env` 读进 Settings、不写回 os.environ）。
    # ytdlp_cmd 给整条命令（如 `python -m yt_dlp`，优先级更高）；ytdlp_exe 给可执行文件路径
    # （路径含空格时用这个）。两者都留空 ⇒ 走 PATH 与常见落地位置。
    ytdlp_exe: str = ""
    ytdlp_cmd: str = ""

    # dashi-ppt 集成：指向包含 package.json 的 dashi-ppt-runtime 项目
    dashi_ppt_project_root: str | None = None
    dashi_ppt_force_fallback: bool = False  # True 时强制走 python-pptx 兜底（测试用）

    @field_validator("ppt_slides_per_point")
    @classmethod
    def _slides_per_point_in_range(cls, v: int) -> int:
        value = int(v)
        if value < 1 or value > 3:
            raise ValueError("PPT_SLIDES_PER_POINT 必须在 1~3 之间")
        return value

    @field_validator("dev_api_key")
    @classmethod
    def _dev_api_key_required_when_auth_on(cls, v: str, info) -> str:
        if info.data.get("auth_enabled") and (not v or v == "dev-key-change-me"):
            raise ValueError("AUTH_ENABLED=true 时必须通过 DEV_API_KEY 设置非默认 API Key")
        return v

    @field_validator(
        "demo_retention_hours",
        "demo_max_generations_per_minute",
        "demo_max_courses_per_day",
        "demo_max_concurrent_jobs",
    )
    @classmethod
    def _demo_limits_positive(cls, v: int) -> int:
        value = int(v)
        if value < 1:
            raise ValueError("演示模式配额必须是正整数")
        return value

    @field_validator("llm_concurrency")
    @classmethod
    def _concurrency_in_range(cls, v: int) -> int:
        value = int(v)
        if value < 1 or value > 4:
            raise ValueError("LLM_CONCURRENCY 必须在 1~4 之间（并发过高会被 LLM 限流）")
        return value

    @field_validator("llm_timeout_s")
    @classmethod
    def _timeout_positive(cls, v: int) -> int:
        value = int(v)
        if value < 1:
            raise ValueError("LLM_TIMEOUT_S 必须为正整数")
        return value

    @property
    def resolved_dashi_project(self) -> str | None:
        """返回可用的 dashi-ppt 项目根目录（配置 > 常见安装路径）。"""
        candidates: list[str | None] = [
            self.dashi_ppt_project_root,
            "C:/Users/wen/_dashi_ppt_review/skills/dashi-ppt/project",
            "C:/Users/wen/.agents/skills/dashi-ppt/project",
        ]
        if self.dashi_ppt_force_fallback:
            return None
        for cand in candidates:
            if cand and (Path(cand) / "package.json").exists():
                return cand
        return None

    @property
    def resolved_frontend_dist(self) -> Path | None:
        """返回可用的前端 dist 目录（配置 > 常见位置）；找不到产物返回 None。"""
        configured = (self.frontend_dist_dir or "").strip()
        if configured:
            cand = Path(configured).expanduser()
            return cand.resolve() if (cand / "index.html").is_file() else None
        repo_root = Path(__file__).resolve().parents[3]  # app/core/config.py → 仓库根
        for cand in (
            repo_root / "frontend" / "dist",
            Path.cwd() / "frontend" / "dist",
            Path.cwd().parent / "frontend" / "dist",
        ):
            if (cand / "index.html").is_file():
                return cand.resolve()
        return None

    def ensure_dirs(self) -> None:
        for d in (
            self.storage_dir,
            self.export_dir,
            self.ppt_work_dir,
            self.storage_dir / "images",
        ):
            d.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            db_dir = Path(self.database_url.replace("sqlite:///", "", 1)).expanduser().resolve().parent
            db_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
