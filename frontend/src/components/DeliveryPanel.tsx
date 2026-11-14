/** M5c 交付包面板（任务 31）：清单预览 + 一键生成 + 下载 + 历史（降级项黄标）。 */
import { useState } from "react";
import { Alert, App, Button, Card, Checkbox, Empty, Space, Switch, Table, Tag, Typography } from "antd";
import { CloudDownloadOutlined, FileZipOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnsType } from "antd/es/table";
import { deliveryApi } from "../deliveryApi";
import { api } from "../api";
import { scenarioLine, scenarioOf } from "../lib/scenarios";
import type {
  DeliveryBuildResult, DeliveryBuildRow, DeliveryConsistency, DeliveryExtraItem, DeliveryPlanItem,
  DeliveryProblemRow, ScormSelfCheck,
} from "../deliveryApi";
import { triggerAuthedDownload } from "../downloadUtils";
/* 任务 83 §B2：时间格式化收敛到唯一出处（原先这个文件里自己写了一份 formatTime）。 */
import { formatLocalTime } from "../lib/datetime";

function humanBytes(n: number): string {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

/** 缺项 / 降级清单：黄标写明原因，绝不静默少文件。 */
function ProblemList({ title, rows, color }: { title: string; rows: DeliveryProblemRow[]; color: string }) {
  if (!rows.length) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <Typography.Text strong>{title}</Typography.Text>
      <ul style={{ margin: "4px 0 0 0", paddingLeft: 20 }}>
        {rows.map((row) => (
          <li key={`${row.item}-${row.reason}`}>
            <Tag color={color}>{row.item}</Tag>
            <Typography.Text type="secondary">{row.reason}</Typography.Text>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function DeliveryPanel({ courseId }: { courseId: string }) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["delivery", courseId],
    queryFn: () => deliveryApi.overview(courseId),
    retry: false,
  });
  // 任务 48z：一致性检查单独请求 —— 它要逐项比对整个笔记库（AI 课 150 项、实测 4.33 秒），
  // 挂在首屏会让面板前几秒看起来像「交付包丢了」（我第 19 轮做 UI 走查就被骗过）。
  // 两个请求并行：清单与历史先渲染，一致性那行稍后自己补上。
  const consistencyQuery = useQuery({
    queryKey: ["deliveryConsistency", courseId],
    queryFn: () => deliveryApi.consistency(courseId),
    retry: false,
  });
  const [last, setLast] = useState<DeliveryBuildResult | null>(null);
  // 任务 48p：交付模式（默认对外 = 出具给客户的包里不含 LLM 用量与金额）。
  // 切换后随下次出包写回课程；null = 还没动过，跟着课程里已保存的模式显示。
  const [external, setExternal] = useState<boolean | null>(null);
  // 任务 86 §B4：学员版讲义（去答案与解析）—— null = **按课程场景自动**（含 self_study 才出）；
  // 勾 / 取消 = 显式覆盖这一次的包。默认八件套结构不变，且这是纯复制的本地渲染（0 LLM）。
  const [studentLectures, setStudentLectures] = useState<boolean | null>(null);
  // 任务 87：按课时打 SCORM 1.2 包（0 LLM；导出前自检，不通过不出包）—— 场景含 lms 才给入口
  const [scorm, setScorm] = useState<{ url: string; self_check: ScormSelfCheck } | null>(null);
  const scormExport = useMutation({
    mutationFn: () => deliveryApi.exportScorm(courseId),
    onSuccess: (row) => {
      setScorm(row);
      message.success(
        `SCORM 1.2 包已生成：${row.self_check.items} 个课时 item / ${row.self_check.files} 个文件`);
    },
    onError: (e) => message.error(`SCORM 导出失败：${String(e)}`),
  });

  const build = useMutation({
    mutationFn: (v: { pdf: "auto" | "off"; allowInconsistent?: boolean; external?: boolean }) =>
      deliveryApi.build(courseId, v.pdf, v.allowInconsistent ?? false, v.external, studentLectures ?? undefined),
    onSuccess: (row) => {
      setLast(row);
      const sync = row.notes_sync;
      if (sync && !sync.synced) {
        message.warning(`已生成 ${row.build_id}：笔记库与网页端仍有 ${sync.inconsistent_after} 项不一致（验收单已标注）`);
      } else {
        message.success(`已生成交付包 ${row.build_id}`);
      }
      qc.invalidateQueries({ queryKey: ["delivery", courseId] });
    },
    onError: (e) => message.error(`生成失败：${String(e)}`),
  });

  const preview = data?.preview;
  // 任务 85 §B3：出包页也要写清这次按什么场景生成（未选 ⇒「未指定场景（按通用口径生成）」）
  const { data: course } = useQuery({
    queryKey: ["course", courseId], queryFn: () => api.getCourse(courseId), retry: false,
  });
  const mode = data?.delivery_mode ?? preview?.delivery_mode;
  const effectiveExternal = external ?? mode?.external ?? true;
  // 与后端同一条默认规则（`services/delivery.py`：`"self_study" in selected`）—— 只在用户
  // 没动过勾选时用；动过就按用户的，不再自作主张。
  const autoStudent = Boolean(scenarioOf(course)?.selected.includes("self_study"));
  // 任务 87：本次会**多出**哪些料（后端按场景算好，界面只显示；默认八件套仍在最上）
  const extras: DeliveryExtraItem[] = preview?.extras ?? [];
  const lmsSelected = Boolean(scenarioOf(course)?.selected.includes("lms"));
  const effectiveStudent = studentLectures ?? autoStudent;
  const consistency: DeliveryConsistency | undefined =
    data?.consistency ?? consistencyQuery.data ?? undefined;
  const builds = data?.builds ?? [];
  const latest = builds[0];

  const countOf = (item: DeliveryPlanItem): number => {
    const counts = preview?.counts;
    if (!counts) return 0;
    switch (item.key) {
      case "lectures":
        return counts.lectures;
      case "decks":
        return counts.decks;
      case "figures":
        return counts.figures;
      case "notes":
        return counts.notes_files;
      case "materials":
        return counts.materials;
      default:
        return 1;
    }
  };

  const previewColumns: ColumnsType<DeliveryPlanItem> = [
    { title: "交付项", dataIndex: "label" },
    {
      title: "数量",
      key: "count",
      width: 90,
      render: (_: unknown, row) => countOf(row),
    },
    {
      title: "状态",
      key: "present",
      width: 120,
      render: (_: unknown, row) =>
        row.present ? <Tag color="green">将包含</Tag> : <Tag color="warning">本次未生成</Tag>,
    },
  ];

  const buildColumns: ColumnsType<DeliveryBuildRow> = [
    {
      title: "构建号 / 生成时间",
      dataIndex: "build_id",
      render: (v: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text code>{v}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {formatLocalTime(row.generated_at)} · {humanBytes(row.zip_bytes)} · LLM {row.llm_calls} 次
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "内容",
      key: "counts",
      render: (_: unknown, row) => (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          讲义 {row.counts?.lectures ?? 0} / 课件 {row.counts?.decks ?? 0} / 图表 {row.counts?.figures ?? 0} /
          文件 {row.counts?.files ?? 0}
        </Typography.Text>
      ),
    },
    {
      title: "缺项 / 降级",
      key: "problems",
      width: 160,
      render: (_: unknown, row) => {
        const n = (row.missing?.length ?? 0) + (row.degraded?.length ?? 0);
        return n ? <Tag color="warning">缺 {row.missing.length} / 降级 {row.degraded.length}</Tag>
          : <Tag color="green">齐全</Tag>;
      },
    },
    {
      title: "操作",
      key: "ops",
      width: 120,
      render: (_: unknown, row) => (
        <Button
          size="small"
          icon={<CloudDownloadOutlined />}
          disabled={!row.zip}
          onClick={() => {
            // 任务 48u：交付包下载接口要鉴权，<a href> 带不了 header ⇒ 走带 key 的下载
            triggerAuthedDownload(deliveryApi.zipUrl(row.build_id), `${row.build_id}.zip`).catch((e) =>
              message.error(`下载失败：${String(e)}`),
            );
          }}
        >
          下载 zip
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="交付包：一次生成、整包交给客户（目录 + 同名 zip）"
        description={
          <Space direction="vertical" size={2}>
            <Typography.Text>
              八件套：00-交付说明 / 01-验收单 / 讲义（每节 PDF + md 源）/ 课件（整课 + 各章 pptx）/
              图表（svg + png）/ 笔记库 / 素材清单.md / manifest.json（每个文件的 sha1 + 生成参数）。
            </Typography.Text>
            <Typography.Text>
              交付模式：
              <Switch
                size="small"
                checked={effectiveExternal}
                onChange={(checked) => setExternal(checked)}
                checkedChildren="对外"
                unCheckedChildren="内部"
                style={{ margin: "0 6px" }}
              />
              {effectiveExternal
                ? "对外交付（默认）：客户包里不含 LLM 用量与金额"
                : "内部复盘：写明 LLM 用量与金额（只给自己看）"}
            </Typography.Text>
            {mode ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                课程当前模式：{mode.label}{external === null ? "" : "（切到上面的开关后，下次出包生效）"}
              </Typography.Text>
            ) : null}
            <Typography.Text>
              <Checkbox
                checked={effectiveStudent}
                onChange={(e) => setStudentLectures(e.target.checked)}
              >
                额外产出「学员版讲义/」（去答案与解析，题目与作答区保留）
              </Checkbox>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {studentLectures === null
                  ? `默认按课程场景：${autoStudent ? "课程含「学生自学」⇒ 本次会出" : "课程不含「学生自学」⇒ 本次不出"}`
                  : "已手动指定，本次按你的勾选"}
              </Typography.Text>
            </Typography.Text>
            <Typography.Text type="secondary">
              构建 = 纯本地渲染与复制：0 次 LLM 调用、不花钱；只读笔记库、不动既有课程内容；
              同一份内容重复构建覆盖同一个包（幂等，跨分钟也复用）。缺哪项会在包里显式写明「本次未生成 + 原因」。
            </Typography.Text>
          </Space>
        }
      />

      <Typography.Text type="secondary" data-testid="scenario-line">
        {scenarioLine(course)}
      </Typography.Text>

      <Card
        size="small"
        loading={isLoading}
        title={<Space><FileZipOutlined />本次将包含{preview ? `（${preview.course_title}）` : ""}</Space>}
        extra={
          <Space>
            <Button type="primary" loading={build.isPending}
              onClick={() => build.mutate({ pdf: "auto", external: effectiveExternal })}>
              一键生成交付包
            </Button>
            <Button loading={build.isPending}
              onClick={() => build.mutate({ pdf: "off", external: effectiveExternal })}>
              只出 HTML（不打 PDF）
            </Button>
          </Space>
        }
      >
        {consistency ? (
          <Alert
            type={consistency.inconsistent ? "warning" : (consistency.ok ? "success" : "info")}
            showIcon
            style={{ marginBottom: 10 }}
            message={consistency.ok === false
              ? "当前一致性：未绑定笔记库，出包时无法自检（笔记库/ 会写成「本次未生成」）"
              : (consistency.inconsistent
                ? `当前一致性：${consistency.inconsistent} / ${consistency.checked} 项不一致（笔记库落后于网页端）`
                : `当前一致性：${consistency.checked} 项全部一致`)}
            description={
              <Space direction="vertical" size={2}>
                <Typography.Text type="secondary">
                  {consistency.hint}。验收单是构建时的快照：内容变过就要重新出包，否则客户看到的数字是旧的。
                </Typography.Text>
                {consistency.inconsistent ? (
                  <Button size="small" danger loading={build.isPending}
                    onClick={() => build.mutate({ pdf: "off", allowInconsistent: true })}>
                    强制出包（允许不一致，验收单会标注）
                  </Button>
                ) : null}
              </Space>
            }
          />
        ) : consistencyQuery.isError ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 10 }}
            message="一致性检查没算出来"
            description={
              <Typography.Text type="secondary">
                {String((consistencyQuery.error as Error)?.message ?? consistencyQuery.error)}
                ；不影响出包 —— 点「一键生成交付包」时后端会再自检一次，不一致会拒绝或先自动同步。
              </Typography.Text>
            }
          />
        ) : (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 10 }}
            message="正在检查笔记一致性…"
            description={
              <Typography.Text type="secondary">
                要逐项比对整个笔记库（AI 课 150 项、约 4 秒）。这一行稍后自己补上，
                不影响下面的清单与历史交付包。
              </Typography.Text>
            }
          />
        )}

        {preview ? (
          <>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              规模：{preview.counts.chapters} 章 / {preview.counts.points} 个知识点 / {preview.counts.lectures} 篇讲义 /
              {preview.counts.decks} 份课件 / {preview.counts.figures} 张图 / {preview.counts.quizzes} 道练习 /
              {preview.counts.concepts} 个概念 / {preview.counts.materials} 个素材
            </Typography.Text>
            <Table<DeliveryPlanItem>
              style={{ marginTop: 8 }}
              rowKey="key"
              size="small"
              pagination={false}
              dataSource={preview.items}
              columns={previewColumns}
            />
            {extras.length ? (
              <div style={{ marginTop: 10 }} data-testid="delivery-extras">
                <Typography.Text strong>按课程场景会多出哪些料</Typography.Text>
                <ul style={{ margin: "4px 0 0 0", paddingLeft: 20 }}>
                  {extras.map((row) => (
                    <li key={row.key}>
                      <Tag color={row.present ? "green" : "default"}>
                        {row.present ? "将包含" : "本次不出"}
                      </Tag>
                      {row.label}
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        （场景：{row.scene}
                        {row.present && row.count ? ` · ${row.count}` : ""}）
                      </Typography.Text>
                    </li>
                  ))}
                </ul>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  判定来自课程「使用场景」（唯一出处见课程页）：含「录成视频课」⇒ 出
                  `录课包/`（逐字稿 / 时长表 / 字幕 / 分镜表）；含「线下课堂面授 / 线上直播」⇒ 多一份互动点；
                  含「学生自学」⇒ 学员版讲义；含「平台投放」⇒ 可按课时打 SCORM 1.2 包。
                  默认八件套仍在最上，场景只会「多加」，不会替换或删减。
                </Typography.Text>
              </div>
            ) : null}
            {lmsSelected ? (
              <Alert
                type="info"
                showIcon
                style={{ marginTop: 10 }}
                data-testid="scorm-entry"
                message="平台投放（SCORM）：将按课时打 SCORM 1.2 包"
                description={
                  <Space direction="vertical" size={4}>
                    <Typography.Text type="secondary">
                      每个课时一个 &lt;item&gt;（标题 + 预计时长 + 知识点 key）；导出前自检
                      imsmanifest 可解析、每个 item 指向的文件都在包里 —— 自检不通过就不出包（0 LLM）。
                    </Typography.Text>
                    <Space>
                      <Button size="small" loading={scormExport.isPending}
                        onClick={() => scormExport.mutate()}>
                        导出 SCORM 包
                      </Button>
                      {scorm ? (
                        <Button size="small" type="primary" icon={<CloudDownloadOutlined />}
                          onClick={() => triggerAuthedDownload(
                            scorm.url, scorm.self_check.file ?? "scorm.zip").catch((e) =>
                            message.error(`下载失败：${String(e)}`))}>
                          下载 SCORM 包
                        </Button>
                      ) : null}
                    </Space>
                    {scorm ? (
                      <Typography.Text type="secondary" data-testid="scorm-self-check">
                        自检通过：{scorm.self_check.items} 个课时 item / {scorm.self_check.files} 个文件 /
                        {humanBytes(scorm.self_check.total_bytes)}
                        {scorm.self_check.estimated_seconds
                          ? ` / 预计总时长 约 ${scorm.self_check.estimated_seconds} 秒` : ""}
                      </Typography.Text>
                    ) : null}
                  </Space>
                }
              />
            ) : null}
            <ProblemList title="本次未生成（原因）" rows={preview.missing} color="warning" />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              PDF 引擎：{preview.pdf.available ? preview.pdf.engine : "未找到 Microsoft Edge → 降级为可直接打印的 HTML"}
              ；流程图：{preview.mermaid.available ? "本地 mermaid 渲染进 PDF" : "降级为代码块（说明里会写明）"}
            </Typography.Text>
          </>
        ) : (
          <Empty description="读取课程信息失败" />
        )}
      </Card>

      {last ? (
        <Alert
          type={last.missing.length + last.degraded.length || last.notes_sync?.synced === false
            ? "warning" : "success"}
          showIcon
          message={`刚生成：${last.build_id}（${humanBytes(last.zip_bytes)} / ${last.files} 个文件 / 用时 ${(
            last.duration_ms / 1000
          ).toFixed(1)} 秒 / PDF ${last.pdf.generated} 篇 / LLM ${last.llm_calls} 次）`}
          description={
            <>
              <Typography.Text code>{last.dir}</Typography.Text>
              <br />
              {last.notes_sync ? (
                <Typography.Text type={last.notes_sync.synced ? "secondary" : "warning"}>
                  构建前已同步笔记：{last.notes_sync.synced ? "是" : "否"}
                  （不一致 {last.notes_sync.inconsistent_after} 项；快照时间
              {" "}{last.notes_sync.snapshot_label || formatLocalTime(last.notes_sync.snapshot_at)}）
                  {last.notes_sync.exported ? "；本次已自动导出一次笔记" : ""}
                </Typography.Text>
              ) : null}
              <br />
              <Button
                size="small"
                type="primary"
                icon={<CloudDownloadOutlined />}
                style={{ marginTop: 8 }}
                onClick={() => {
                  triggerAuthedDownload(deliveryApi.zipUrl(last.build_id), `${last.build_id}.zip`).catch((e) =>
                    message.error(`下载失败：${String(e)}`),
                  );
                }}
              >
                下载 zip
              </Button>
              <ProblemList title="本次未生成（原因）" rows={last.missing} color="warning" />
              <ProblemList title="降级项（原因）" rows={last.degraded} color="gold" />
            </>
          }
        />
      ) : null}

      <Card
        size="small"
        title="历史交付包"
        extra={
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {/* 任务 48z：没加载完不许说「还没有生成过」—— 那正是「以为包丢了」的来源 */}
        上次生成：{isLoading ? "读取中…" : latest ? formatLocalTime(latest.generated_at) : "还没有生成过"}
          </Typography.Text>
        }
      >
        <Table<DeliveryBuildRow>
          rowKey="build_id"
          size="small"
          pagination={false}
          loading={isLoading}
          dataSource={builds}
          columns={buildColumns}
          locale={{
            emptyText: isLoading
              ? <Empty description="正在读取历史交付包…" />
              : <Empty description="还没有交付包：点右上角「一键生成交付包」" />,
          }}
        />
      </Card>
    </Space>
  );
}
