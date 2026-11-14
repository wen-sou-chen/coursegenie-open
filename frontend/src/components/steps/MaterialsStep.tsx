import { useEffect, useRef, useState } from "react";
import SectionCard from "../ui/SectionCard";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, Button, Checkbox, Empty, Form, Input, List, Modal, Popconfirm, Space, Tag, Typography, Upload,
} from "antd";
import { FileAddOutlined, LinkOutlined, RobotOutlined, UploadOutlined } from "@ant-design/icons";
import { api } from "../../api";
import type { CapabilityItem } from "../../api";
import { requestAutoSync } from "../../syncNotes";
import type { MaterialAsr, MaterialFull, MaterialLink, MaterialRow } from "../../types";

const FULL_PAGE = 2000;
/** 任务 69 §B1.5.3：按段落读时的窗口大小（段号定位用，不按字数分页）。 */
const FULL_PAGE_PARAS = 200;
/** 解析出的字数低于这个值 → 提醒老师核对原件（扫描版 PPT/PDF 会抽不到字）。 */
const LOW_CHARS = 200;
/** 任务 54：OCR 平均置信度低于该值即提示人工抽查（与后端 OCR_CONFIDENCE_HINT 一致）。 */
const OCR_LOW = 0.75;

/**
 * 任务 102：秒 → `12:03` / `1:02:03`（媒体时长的展示口径）。
 *
 * 这是**时长**不是时间戳 —— 与任务 83/84 的「时间戳必须走本地时区」那套口径无关，
 * 所以不走 `formatTime`；素材页里的 `[mm:ss]` 也由后端同一算法生成。
 */
function fmtDuration(sec?: number | null): string {
  const total = Math.max(0, Math.round(Number(sec ?? 0)));
  const mm = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  const hh = Math.floor(total / 3600);
  return hh ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`;
}

/**
 * 任务 102：音视频素材的转写状态（`asr` = 后端 `meta.asr`）。
 *
 * `jobId` 有值时轮询 `GET /content/jobs/{job_id}` 显示进度（= 已转写时长 ÷ 总时长）；
 * 转完 / 失败后停轮询并让父组件刷新素材列表（正文那时才落库）。转写中、跳过、失败
 * 三种状态**说法必须不一样**（用户才看得出是「在跑」还是「没跑成」）。
 */
function AsrStatusTag({ asr, jobId, onSettled }: {
  asr?: MaterialAsr | null;
  jobId?: string;
  onSettled: () => void;
}) {
  const { data } = useQuery({
    queryKey: ["asrJob", jobId],
    queryFn: () => api.getJobStatus(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "success" || status === "failed" ? false : 2000;
    },
  });
  const settled = data?.status === "success" || data?.status === "failed";
  useEffect(() => {
    if (settled) onSettled();
    // 只在「转完 / 失败」这一刻通知一次；onSettled 每次渲染都是新函数，不进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled]);
  const status = asr?.status ?? "";
  if (status === "pending" || status === "running" || (jobId && !settled)) {
    const progress = Number((data?.result as { progress?: number } | undefined)?.progress ?? 0);
    return (
      <Tag color="blue">
        本机转写中{jobId ? ` · ${Math.round(progress * 100)}%` : "…"}
        {asr?.duration_sec != null ? ` · 时长 ${fmtDuration(asr.duration_sec)}` : ""}
      </Tag>
    );
  }
  if (status === "done") {
    return asr?.skipped_reason ? (
      <Tag color="orange">{asr.skipped_reason}</Tag>
    ) : (
      <Tag color="geekblue">
        本机转写 {asr?.model ?? ""} · 时长 {fmtDuration(asr?.duration_sec)}
        {` · ${asr?.segments ?? 0} 段`}
        {asr?.avg_logprob != null ? ` · 平均置信度 ${asr.avg_logprob}` : ""}
      </Tag>
    );
  }
  if (status) return <Tag color="orange">{asr?.skipped_reason || "转写未完成"}</Tag>;
  return null;
}

/**
 * P2-1：展开看素材的解析全文（文件型也能看）。
 *
 * 后端原先只有「只支持文本素材」的接口，上传 PPT 后点展开会 404；
 * 这里改用分页的 `GET /api/material/full/{id}`，长文不会一次全塞进来。
 */
function MaterialFullText({ id, target }: { id: string; target?: number }) {
  // target != null ⇒ 走「按段落」模式：AI 补充卡片上「第 N 段」点进来要落到**同一段**
  // （[N] 与注入/素材页同口径，见后端 get_material_full(numbered=True)）。
  const numbered = target != null;
  const [offset, setOffset] = useState(numbered ? Math.max(0, (target ?? 1) - 1) : 0);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const { data, isLoading } = useQuery({
    queryKey: ["materialFull", id, offset, numbered],
    queryFn: () => api.getMaterialFull(id, numbered
      ? { offset, limit: FULL_PAGE_PARAS, numbered: true }
      : { offset, limit: FULL_PAGE }),
  });
  useEffect(() => {
    if (!numbered || !data) return;
    const node = boxRef.current?.querySelector(`[data-para="${target}"]`);
    if (node) node.scrollIntoView({ block: "center" });
  }, [numbered, target, data]);
  if (isLoading && !data) return <Typography.Text type="secondary">正在读取解析全文…</Typography.Text>;
  if (!data) return null;
  if (numbered) {
    const paras = data.paragraphs ?? [];
    return (
      <div style={{ marginTop: 8 }}>
        <Space wrap style={{ marginBottom: 6 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            按段落核对（与注入 / 素材页同一个 [N]）· 共 {data.total_paragraphs ?? 0} 段
            {data.has_more ? ` · 当前 ${data.offset + 1}-${data.offset + paras.length}` : ""}
          </Typography.Text>
          <Tag color="blue">目标：第 {target} 段</Tag>
        </Space>
        <div ref={boxRef} style={{
          maxHeight: 260, overflowY: "auto", border: "1px solid #f0f0f0",
          borderRadius: 6, padding: "6px 8px",
        }}>
          {paras.map((p) => (
            <div key={p.n} data-para={p.n} style={{
              fontSize: 12, padding: "2px 6px", whiteSpace: "pre-wrap",
              background: p.n === target ? "#fff1f0" : undefined,
              borderLeft: p.n === target ? "3px solid #ff4d4f" : "3px solid transparent",
            }}>
              <Typography.Text type="secondary">[{p.n}]</Typography.Text> {p.text}
            </div>
          ))}
        </div>
        <Space style={{ marginTop: 6 }}>
          <Button size="small" disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - FULL_PAGE_PARAS))}>
            上一页
          </Button>
          <Button size="small" disabled={!data.has_more}
            onClick={() => setOffset(data.next_offset ?? offset)}>
            下一页
          </Button>
        </Space>
      </div>
    );
  }
  const total = data.total_chars;
  return (
    <div style={{ marginTop: 8 }}>
      <Space wrap style={{ marginBottom: 6 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          解析整文 {total} 字
          {data.pages ? ` · ${data.pages} 页` : ""}
          {total > FULL_PAGE ? ` · 当前显示 ${data.offset + 1}-${data.offset + data.text.length}` : ""}
        </Typography.Text>
        {data.truncated ? (
          <Tag color="orange">原件解析出 {data.raw_chars} 字，只存了前 {total} 字</Tag>
        ) : null}
        {total < LOW_CHARS ? (
          <Tag color="orange">解析出的字很少，建议核对原件（扫描版图片提不到文字）</Tag>
        ) : null}
        {data.ocr?.skipped ? (
          <Tag color="orange">{data.ocr.skipped}</Tag>
        ) : data.ocr ? (
          <Tag color={(data.ocr.confidence ?? 1) < OCR_LOW ? "orange" : "purple"}>
            OCR {data.ocr.lines ?? 0} 行
            {data.ocr.confidence != null ? ` · 平均置信度 ${data.ocr.confidence}` : ""}
            {data.ocr.confidence != null && data.ocr.confidence < OCR_LOW ? "（偏低，建议人工抽查）" : ""}
          </Tag>
        ) : null}
        {/* 任务 102：视频 / 录音的转写信息（引擎与模型、时长、段数、平均置信度）或跳过原因 */}
        {data.asr ? (
          data.asr.status === "done" && !data.asr.skipped_reason ? (
            <Tag color="geekblue">
              本机语音转写 {data.asr.model ?? ""} · 时长 {fmtDuration(data.asr.duration_sec)}
              {` · ${data.asr.segments ?? 0} 段`}
              {data.asr.avg_logprob != null ? ` · 平均置信度 ${data.asr.avg_logprob}` : ""}
            </Tag>
          ) : (
            <Tag color="orange">
              {data.asr.status === "done"
                ? data.asr.skipped_reason
                : data.asr.skipped_reason || (data.asr.status === "pending" || data.asr.status === "running"
                  ? "正在本机转写…" : "转写未完成")}
            </Tag>
          )
        ) : null}
      </Space>
      <Input.TextArea
        value={data.text}
        readOnly
        autoSize={{ minRows: 4, maxRows: 16 }}
        style={{ fontSize: 12 }}
      />
      <Space style={{ marginTop: 6 }}>
        <Button size="small" disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - FULL_PAGE))}>
          上一页
        </Button>
        <Button size="small" disabled={!data.has_more}
          onClick={() => setOffset(data.next_offset ?? offset)}>
          下一页
        </Button>
      </Space>
    </div>
  );
}
const AI_DIRECTIONS = [
  { label: "概念铺垫", value: "concept" },
  { label: "例题练习", value: "examples" },
  { label: "学情导入", value: "learner" },
  { label: "难点解析", value: "difficulty" },
  { label: "背景拓展", value: "background" },
  { label: "自定义（下方填写要求）", value: "custom" },
];
/** 后端 AI 补充素材的引用前缀（backend/app/services/material.py::AI_TITLE_PREFIX，两处必须一致）。 */
const AI_TITLE_PREFIX = "AI补充——";
/**
 * 任务 118：从链接导入的状态（pending 排队 / 抓取中带进度 / 失败写人话 / 完成写「用了哪条路」）。
 *
 * 「从链接导入」是**异步**的（播放列表可能跑很久），所以进度走**既有** `GET /content/jobs/{id}`
 * 那条链（与音视频转写同一个 job 口径，不新造第二套进度机制）；失败原因直接显示后端写的人话。
 */
function LinkStatusTag({ link, jobId, onSettled }: {
  link?: MaterialLink | null;
  jobId?: string;
  onSettled: () => void;
}) {
  const { data } = useQuery({
    queryKey: ["linkJob", jobId],
    queryFn: () => api.getJobStatus(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "success" || status === "failed" ? false : 2000;
    },
  });
  const settled = data?.status === "success" || data?.status === "failed";
  useEffect(() => {
    if (settled) onSettled();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled]);
  if (!link) return null;
  if (link.status === "pending" || (jobId && !settled)) {
    const progress = Number((data?.result as { progress?: number } | undefined)?.progress ?? 0);
    return <Tag color="blue">从链接抓取中{jobId ? ` · ${Math.round(progress * 100)}%` : "…"}</Tag>;
  }
  if (link.status === "failed") {
    return <Tag color="red">从链接导入失败：{link.skipped_reason || "原因见下方"}</Tag>;
  }
  const pathLabel = link.path === "subtitle" ? "取自字幕"
    : link.path === "asr" ? "本机转写"
      : link.path === "mixed" ? "字幕 + 本机转写" : "—";
  return (
    <Tag color="geekblue">
      从链接导入 · {pathLabel} · {link.videos ?? 0} 条
      {link.language ? ` · ${link.language}` : ""}
    </Tag>
  );
}

export default function MaterialsStep({ courseId, onNext }: { courseId: string; onNext: () => void }) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  const [form] = Form.useForm();
  const [pasteOpen, setPasteOpen] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const [aiForm] = Form.useForm();
  const [editOpen, setEditOpen] = useState(false);
  const [fullId, setFullId] = useState("");
  /** 任务 69 §B1.5.3：跳到原素材的第 N 段核对（与 fullId 一起用；number≠undefined 走按段落模式）。 */
  const [fullPara, setFullPara] = useState<number | undefined>(undefined);
  /** 展开哪条 AI 补充素材的「有来处 / 无来处」明细。 */
  const [groundId, setGroundId] = useState("");
  const [editForm] = Form.useForm();
  const { data: materials = [], isLoading: materialsLoading } = useQuery({
    queryKey: ["materials", courseId],
    queryFn: () => api.listMaterials(courseId),
    // 任务 102：有音视频正在转写时每 5s 自刷一次 —— 转完的正文与 asr 状态要自己浮出来
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      return rows.some((m) => m.asr?.status === "pending" || m.asr?.status === "running")
        ? 5000 : false;
    },
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["materials", courseId] });
  /** 任务 102：素材 id → 转写作业 id（有它才轮询得到百分比；转完即清）。 */
  const [asrJobs, setAsrJobs] = useState<Record<string, string>>({});
  const clearAsrJob = (id: string) => setAsrJobs((prev) => {
    const next = { ...prev };
    delete next[id];
    return next;
  });

  const [linkOpen, setLinkOpen] = useState(false);
  const [linkForm] = Form.useForm();
  /** 任务 118：素材 id → 抓取作业 id（有它才轮询得到百分比；抓完即清）。 */
  const [linkJobs, setLinkJobs] = useState<Record<string, string>>({});
  const clearLinkJob = (id: string) => setLinkJobs((prev) => {
    const next = { ...prev };
    delete next[id];
    return next;
  });

  /** 任务 118：能力清单（缺 yt-dlp / JS 运行时时在弹窗里直接说清「缺什么、影响什么、怎么装」）。 */
  const { data: caps } = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => api.health(),
    staleTime: 60000,
  });
  const linkCapItems: CapabilityItem[] = (caps?.capabilities?.items ?? [])
    .filter((item) => item.key === "ytdlp" || item.key === "js_runtime");
  const linkMissing = linkCapItems.filter((item) => !item.available);
  const upload = useMutation({
    mutationFn: (f: File) => api.uploadMaterial(courseId, f),
    onSuccess: (res) => {
      if (res.job_id) setAsrJobs((prev) => ({ ...prev, [res.id]: String(res.job_id) }));
      message.success(res.job_id
        ? "已上传：视频/录音正在本机转写，进度见素材行"
        : "已上传并提取素材");
      refresh();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const paste = useMutation({
    mutationFn: (v: { title: string; text: string }) =>
      api.pasteMaterial({ course_id: courseId, title: v.title, text: v.text }),
    onSuccess: () => { message.success("已加入素材"); setPasteOpen(false); form.resetFields(); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const aiGen = useMutation({
    mutationFn: (v: { directions: string[]; custom_instruction?: string }) =>
      api.generateMaterials({
        course_id: courseId,
        directions: v.directions,
        custom_instruction: v.custom_instruction ?? "",
      }),
    onSuccess: () => {
      message.success("AI 补充素材已生成，可在列表里编辑或删除");
      setAiOpen(false);
      aiForm.resetFields();
      refresh();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const del = useMutation({
    mutationFn: (id: string) => api.deleteMaterial(id),
    onSuccess: () => { message.success("已删除素材"); refresh(); },
    onError: (e) => message.error(String(e)),
  });
  // 任务 69 §B1.5.1：上传原件的「一键清理」—— **只有老师点了**才会改写库里的正文
  const clean = useMutation({
    mutationFn: (id: string) => api.cleanMaterial(id),
    onSuccess: (res) => {
      message.success(res.cleaned_lines
        ? `已清理 ${res.cleaned_lines} 行（库里的正文已更新）`
        : "没有可清理的行：检测到的内容剥完会清空正文，已按原样保留");
      refresh();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  // 任务 72 §B2：老素材的页数 / 来源 / 外壳行数由**这个按钮**显式补齐（GET 只读，不再静默写库）
  const backfill = useMutation({
    mutationFn: () => api.backfillMaterialMeta(courseId),
    onSuccess: (res) => {
      message.success(res.backfilled
        ? `已补齐 ${res.backfilled} 份素材的页数/来源信息`
        : "素材信息已是最新，没有需要补齐的");
      if (res.backfilled) refresh();
    },
    onError: (e) => message.error(String(e)),
  });
  // 任务 102：音视频的「重新转写」—— 显式入口（跳过 / 失败 / 换了模型后重跑）
  const retranscribe = useMutation({
    mutationFn: (id: string) => api.transcribeMaterial(id),
    onSuccess: (res, id) => {
      if (res.job_id) setAsrJobs((prev) => ({ ...prev, [id]: String(res.job_id) }));
      message.success("已提交转写：视频/录音在本机跑，完成后自动刷新");
      refresh();
    },
    onError: (e) => message.error(String(e)),
  });
  // 任务 118：从链接导入（YouTube 字幕优先 / 没字幕转本机转写）—— 素材的第三种来源
  const linkImport = useMutation({
    mutationFn: (v: { url: string; title?: string; cookies?: string }) =>
      api.importMaterialLink({ course_id: courseId, url: v.url, title: v.title, cookies: v.cookies }),
    onSuccess: (res) => {
      if (res.job_id) setLinkJobs((prev) => ({ ...prev, [res.id]: String(res.job_id) }));
      if (res.job_id) {
        message.success("已开始抓取：先抓字幕，没字幕才下音频在本机转写（进度见素材行）");
      } else {
        message.warning(String(res.message ?? "没能抓取，原因见素材行"));
      }
      if (res.warning) message.warning(String(res.warning));
      setLinkOpen(false);
      linkForm.resetFields();
      refresh();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  // 任务 118：抓失败了再点一次（装好 yt-dlp / 换一份新 cookie 之后）—— 链接存在素材行里
  const reimportLink = useMutation({
    mutationFn: (id: string) => api.reimportMaterialLink(id),
    onSuccess: (res, id) => {
      if (res.job_id) setLinkJobs((prev) => ({ ...prev, [id]: String(res.job_id) }));
      message.success("已重新排队抓取，进度见素材行");
      refresh();
    },
    onError: (e) => message.error(String(e)),
  });

  const editSave = useMutation({
    mutationFn: (v: { id: string; title: string; text: string }) =>
      api.updateMaterial(v.id, { title: v.title, text: v.text }),
    onSuccess: () => { message.success("素材已更新"); setEditOpen(false); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const openEdit = async (row: MaterialRow) => {
    try {
      const full = await api.getMaterialText(row.id);
      editForm.setFieldsValue({ __id: row.id, title: full.title, text: full.text });
      setEditOpen(true);
    } catch (e) {
      message.error(String(e));
    }
  };
  /** 任务 69 §B1.5.3：AI 补充的「第 N 段」→ 打开原素材并按段落定位（标题有/无 AI补充—— 前缀都认）。 */
  const jumpTo = (materialName: string, para: number) => {
    const hit = materials.find((x) => x.title === materialName
      || `${AI_TITLE_PREFIX}${x.title}` === materialName);
    if (!hit) {
      message.warning("没找到这条依据对应的素材（可能已被删除）");
      return;
    }
    setFullId(hit.id);
    setFullPara(para);
  };
  const submitAi = () => {
    aiForm.validateFields().then((v: { directions: string[]; custom_instruction?: string }) => {
      const dirs = v.directions ?? [];
      if (!dirs.length) {
        message.warning("请至少勾选一个补充方向");
        return;
      }
      if (dirs.includes("custom") && !String(v.custom_instruction ?? "").trim()) {
        message.warning("勾选了自定义方向，请填写具体补充要求");
        return;
      }
      aiGen.mutate(v);
    });
  };
  if (materialsLoading) {
    return <SectionCard loading style={{ minHeight: 220 }} />;
  }
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="第 1 步 · 准备原始资料"
        description="把备课讲义、PPT、PDF、Word、图片，或视频 / 录音，以及直接粘贴的文本放进来即可；素材会自动作为生成大纲与正文的依据。视频与录音在本机转写，不上传云端（转写要排队跑，时长越长等得越久）；没有现成资料也可以让 AI 按方向补充，或直接点「下一步」从课程标题开始生成。"
      />
      <SectionCard
        title={`原始资料（${materials.length}）`}
        extra={
          <Space wrap>
            <Upload
              accept=".md,.txt,.csv,.markdown,.docx,.pdf,.pptx,.png,.jpg,.jpeg,.webp,.mp4,.mov,.mkv,.webm,.avi,.m4v,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus"
              multiple
              showUploadList={false}
              beforeUpload={(f) => { upload.mutate(f); return false; }}
            >
              <Button type="primary" icon={<UploadOutlined />}>上传文件</Button>
            </Upload>
            <Button icon={<FileAddOutlined />} onClick={() => setPasteOpen(true)}>粘贴文本</Button>
            <Button icon={<LinkOutlined />} onClick={() => setLinkOpen(true)}>从链接导入</Button>
            <Button icon={<RobotOutlined />} onClick={() => setAiOpen(true)}>AI 补充资料</Button>
            {/* 任务 72 §B2：老素材（页数 / 来源 / 外壳行数没落库）的**显式**补齐入口 ——
                修前这一步藏在「打开素材页」的 GET 里，会悄悄改库 mtime（离奇且不可回查）。 */}
            <Button
              disabled={!materials.length}
              loading={backfill.isPending}
              title="素材信息不全会显示「—」：点它一次性补齐页数 / 来源 / 外壳行数（只补元信息，不改正文）"
              onClick={() => backfill.mutate()}
            >
              补齐素材信息
            </Button>
            <Button disabled={!materials.length} onClick={onNext}>下一步：生成大纲</Button>
          </Space>
        }
      >
        {materials.length === 0 ? (
          <Empty description="还没有资料：上传文件 / 粘贴文本 / 让 AI 先补一版" />
        ) : (
          <List
            size="small"
            dataSource={materials}
            renderItem={(m) => (
              <List.Item
                actions={[
                  m.kind === "text" && (
                    <a key="edit" onClick={() => openEdit(m)}>编辑</a>
                  ),
                  <a key="full" onClick={() => {
                    setFullPara(undefined);
                    setFullId(fullId === m.id ? "" : m.id);
                  }}>
                    {fullId === m.id ? "收起全文" : "看解析全文"}
                  </a>,
                  m.asr && (
                    <a key="asr" onClick={() => retranscribe.mutate(m.id)}>
                      {m.asr.status === "done" ? "重新转写" : "重试转写"}
                    </a>
                  ),
                  m.link && (
                    <a key="link" onClick={() => reimportLink.mutate(m.id)}>
                      {m.link.status === "done" ? "重新导入" : "重试导入"}
                    </a>
                  ),
                  <Popconfirm key="del" title="删除该素材？" onConfirm={() => del.mutate(m.id)}>
                    <a>删除</a>
                  </Popconfirm>,
                ]}
              >
                <div style={{ width: "100%" }}>
                  <Space wrap>
                    <Tag color={m.kind === "file" ? "blue" : m.kind === "image" ? "purple"
                      : m.kind === "video" ? "geekblue" : m.kind === "audio" ? "cyan" : "green"}>
                      {m.kind === "file" ? (m.ext || "文件") : m.kind === "image" ? (m.ext || "图片")
                        : m.kind === "video" || m.kind === "audio" ? (m.ext || "音视频") : "文本"}
                    </Tag>
                    {/* 任务 69 §B1.5.1：来源类型（人工上传 / 粘贴文本(疑似 AI 产物) / AI 补充）——
                        它取代了原先只给 AI 素材的那枚紫色「AI 补充」标签（两枚一起渲染会重出一个）。 */}
                    {m.origin_label ? (
                      <Tag color={m.origin === "ai" ? "purple" : m.origin === "paste" ? "gold" : undefined}>
                        {m.origin_label}
                      </Tag>
                    ) : null}
                    <Typography.Text strong>{m.title}</Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      解析 {m.chars} 字{m.pages ? ` · ${m.pages} 页` : ""}
                      {m.original ? ` · ${m.original}` : ""}
                    </Typography.Text>
                    {m.kind === "image" && m.ocr?.skipped ? (
                      <Typography.Text type="warning" style={{ fontSize: 12 }}>
                        {m.ocr.skipped}
                      </Typography.Text>
                    ) : null}
                    {/* 任务 69 §B1.5.3：AI 补充的「有来处 / 无来处」—— 无来处的条目展开标红 */}
                    {m.source === "ai" ? (
                      <Tag color={(m.ungrounded_count ?? 0) > 0 ? "red" : "green"}
                        style={{ cursor: "pointer" }}
                        onClick={() => setGroundId(groundId === m.id ? "" : m.id)}>
                        AI 补充 · 有依据 {m.grounded_count ?? 0} 条 / 无依据 {m.ungrounded_count ?? 0} 条
                        {groundId === m.id ? "（收起）" : "（看依据）"}
                      </Tag>
                    ) : null}
                    {/* 三种状态的说法**必须不一样**（任务 72 §B1，用户点名：别用同一个词）：
                        ① 上传原件（must_clean=false）：检测到 N 行…（按口径保持原样）+ 一键清理；
                        ② 必须干净但库内没落库（老数据，shell_stripped=false）：出库时会剥离 N 行
                           （库内保留原文）+ 一键清理（点了才写库）；
                        ③ 已落库（shell_stripped=true）：已清理 N 行（库内正文已更新）。
                        判据是 shell_stripped，不是 cleaned_lines —— 老数据的 cleaned_lines 是
                        现算出来的「出库会剥几行」，拿它当「已清理」会说反（实测素材 6 踩过）。 */}
                    {!m.must_clean && !m.cleaned_lines && (m.detected_lines ?? 0) > 0 ? (
                      <Space size={4}>
                        <Tag color="orange" title={(m.detected_rules || []).join("、")}>
                          检测到 {m.detected_lines} 行疑似页眉页脚 / 对话外壳（按口径保持原样）
                        </Tag>
                        <Popconfirm
                          title={`清掉这 ${m.detected_lines} 行？会改写这份素材的正文`}
                          onConfirm={() => clean.mutate(m.id)}
                        >
                          <a>一键清理</a>
                        </Popconfirm>
                      </Space>
                    ) : null}
                    {!m.shell_stripped && (m.cleaned_lines ?? 0) > 0 ? (
                      <Space size={4}>
                        <Tag color="geekblue" title={(m.cleaned_rules || []).join("、")}>
                          出库时会剥离 {m.cleaned_lines} 行对话外壳 / 页眉（库内保留原文）
                        </Tag>
                        <Popconfirm
                          title={`把剥离结果落库？会改写这份素材的正文（清掉这 ${m.cleaned_lines} 行）`}
                          onConfirm={() => clean.mutate(m.id)}
                        >
                          <a>一键清理</a>
                        </Popconfirm>
                      </Space>
                    ) : null}
                    {m.shell_stripped && (m.cleaned_lines ?? 0) > 0 ? (
                      <Tag color="cyan" title={(m.cleaned_rules || []).join("、")}>
                        已清理 {m.cleaned_lines} 行对话外壳 / 页眉（库内正文已更新）
                      </Tag>
                    ) : null}
                    {/* 剥不了（如「剥完会清空正文」）⇒ 只提示原因，绝不硬改 */}
                    {!m.cleaned_lines && (m.cleaned_rules || []).length > 0 ? (
                      <Tag color="orange">{(m.cleaned_rules || []).join("、")}</Tag>
                    ) : null}
                    {m.kind === "image" && !m.ocr?.skipped ? (
                      <Tag color={(m.ocr?.confidence ?? 1) < OCR_LOW ? "orange" : "purple"}>
                        图片 · OCR {m.ocr?.lines ?? 0} 行
                        {(m.ocr?.confidence ?? 1) < OCR_LOW ? "（置信度偏低，建议抽查）" : ""}
                      </Tag>
                    ) : null}
                    {/* 任务 102：音视频转写完 / 没识别到语音时，同样复用「字数偏少」这条提示 */}
                    {(m.kind === "file" || m.kind === "video" || m.kind === "audio") && m.chars < LOW_CHARS ? (
                      <Tag color="orange">解析字数偏少，建议核对原件</Tag>
                    ) : null}
                    {/* 任务 102：视频 / 录音的转写状态（转写中带进度、跳过写原因、转完写引擎与模型） */}
                    <AsrStatusTag asr={m.asr} jobId={asrJobs[m.id]}
                      onSettled={() => { refresh(); clearAsrJob(m.id); }} />
                    <LinkStatusTag link={m.link} jobId={linkJobs[m.id]}
                      onSettled={() => { refresh(); clearLinkJob(m.id); }} />
                  </Space>
                  {groundId === m.id ? (
                    <div style={{ marginTop: 6, fontSize: 12 }}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        有依据的条目可一键跳到原素材核对；无来处的条目**不能当事实依据**（讲义里会标
                        「（无直接依据,建议人工确认）」）。
                      </Typography.Text>
                      {(m.grounded || []).map((g, i) => (
                        <div key={`g${i}`} style={{ marginTop: 4 }}>
                          <Typography.Text style={{ fontSize: 12 }}>{g.claim}</Typography.Text>{" "}
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            ←（来源:{g.material} 第 {g.para} 段）
                          </Typography.Text>{" "}
                          <a onClick={() => jumpTo(g.material, g.para)}>跳到原文核对</a>
                        </div>
                      ))}
                      {(m.ungrounded || []).map((u, i) => (
                        <div key={`u${i}`} style={{ marginTop: 4, color: "#cf1322" }}>
                          <Typography.Text type="danger" style={{ fontSize: 12 }}>
                            {u}（无依据·模型补充 —— 上课前请人工确认）
                          </Typography.Text>
                        </div>
                      ))}
                      {!(m.grounded || []).length && !(m.ungrounded || []).length ? (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          这条补充素材里没有解析到依据标注（老数据或模型没按格式写）—— 建议人工核对。
                        </Typography.Text>
                      ) : null}
                    </div>
                  ) : null}
                  {fullId === m.id ? <MaterialFullText id={m.id} target={fullPara} /> : null}
                </div>
              </List.Item>
            )}
          />
        )}
      </SectionCard>
      <Modal
        title="粘贴原始资料"
        open={pasteOpen}
        onCancel={() => setPasteOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={paste.isPending}
        width={760}
      >
        <Form form={form} layout="vertical" onFinish={(v) => paste.mutate(v)}>
          <Form.Item name="title" label="标题" rules={[{ required: true }]}>
            <Input placeholder="如：光合作用备课讲义" />
          </Form.Item>
          <Form.Item name="text" label="正文" rules={[{ required: true }]}>
            <Input.TextArea rows={12} placeholder="粘贴你的讲义 / 教案 / 资料原文…" />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="AI 补充资料"
        open={aiOpen}
        onCancel={() => setAiOpen(false)}
        onOk={submitAi}
        okText="生成补充素材"
        confirmLoading={aiGen.isPending}
        width={640}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="按需补充，不整段硬塞"
          description={"只勾选你缺的方向；AI 会结合课程标题与已有素材生成，结果进入素材列表，可编辑或删除。"
            + "生成时**强制接地**：每条事实性表述都要给出来处（（来源:《素材标题》第 N 段）），"
            + "给不出来处的会标成「(无依据·模型补充)」并在卡片里标红 —— 它不算事实依据。"}
        />
        <Form form={aiForm} layout="vertical" initialValues={{ directions: ["concept"] }}>
          <Form.Item name="directions" label="需要补充的方向">
            <Checkbox.Group options={AI_DIRECTIONS} style={{ display: "flex", flexDirection: "column", gap: 8 }} />
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(prev, cur) => prev.directions !== cur.directions}
          >
            {({ getFieldValue }) =>
              (getFieldValue("directions") ?? []).includes("custom") && (
                <Form.Item name="custom_instruction" label="自定义补充要求">
                  <Input.TextArea rows={3} placeholder="如：补充与本地生活相关的导入案例" />
                </Form.Item>
              )
            }
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="编辑素材"
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        onOk={() => editForm.submit()}
        confirmLoading={editSave.isPending}
        width={760}
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={(v) => {
            const id = editForm.getFieldValue("__id");
            if (id) editSave.mutate({ id, title: v.title, text: v.text });
          }}
        >
          <Form.Item name="title" label="标题" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="text" label="正文" rules={[{ required: true }]}>
            <Input.TextArea rows={14} />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="从链接导入（YouTube）"
        open={linkOpen}
        onCancel={() => setLinkOpen(false)}
        onOk={() => linkForm.submit()}
        confirmLoading={linkImport.isPending}
        width={760}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="先抓字幕，没字幕才下音频在本机转写（不调模型、不花钱）"
          description="粘贴 YouTube 视频或播放列表链接。抓取是排队跑的（每请求 ≥4 秒、同一列表内 ≥20 秒），进度见素材行；抓完会像其它素材一样作为生成大纲与正文的依据。"
        />
        {linkMissing.length ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="本机缺「从链接导入」需要的外部组件"
            description={(
              <div>
                {linkMissing.map((item) => (
                  <div key={item.key}>
                    <b>{item.label}</b>：{item.reason}　　装法：{item.install}
                  </div>
                ))}
              </div>
            )}
          />
        ) : null}
        <Form form={linkForm} layout="vertical" onFinish={(v) => linkImport.mutate(v)}>
          <Form.Item name="url" label="链接" rules={[{ required: true }]}>
            <Input placeholder="https://www.youtube.com/watch?v=… 或 …/playlist?list=…" />
          </Form.Item>
          <Form.Item name="title" label="标题（可留空）">
            <Input placeholder="留空就用 YouTube 上的标题" />
          </Form.Item>
          <Form.Item
            name="cookies"
            label="cookies.txt 路径（可选）"
            extra="公开视频通常不用；遇到「Sign in to confirm you're not a bot」时才需要一份 youtube.com 的 cookie。系统只把这个路径交给 yt-dlp：不复制、不进日志、不写进课程数据；cookie 会过期，失效时提示会告诉你重新导出一份。"
          >
            <Input placeholder="C:\Users\…\cookies.txt" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
