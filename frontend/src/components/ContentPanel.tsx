import { useMemo, useState } from "react";
import SectionCard from "./ui/SectionCard";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, Button, Collapse, Empty, Form, Input, List, Modal, Popconfirm, Select, Space,
  Spin, Tag, Typography, Upload, message,
} from "antd";
import { PlusOutlined, ReloadOutlined, UploadOutlined } from "@ant-design/icons";
import { api } from "../api";
import { requestAutoSync } from "../syncNotes";
import MdWithFlow from "./MdWithFlow";
import { useDiagramMaps } from "../diagramMaps";
import type { ContentRow } from "../types";
function imgSrc(row: ContentRow): string {
  const path = String(row.body.path ?? "");
  return path ? `/${path}` : "";
}
interface ImageDraft { prompt: string; size: string; itemId?: string; nodeId?: string }
interface FlowDraft { description: string; kind: string; itemId?: string; nodeId?: string }
interface MermaidDraft { title: string; mermaid: string; itemId?: string; nodeId?: string }
export default function ContentPanel({ courseId }: { courseId: string }) {
  const qc = useQueryClient();
  const [form] = Form.useForm();
  const { data: rows = [], refetch } = useQuery({
    queryKey: ["content", courseId],
    queryFn: () => api.listContent(courseId),
  });
  const { data: plan } = useQuery({
    queryKey: ["plan", courseId],
    queryFn: () => api.getPlan(courseId),
    retry: false,
  });
  const { data: course } = useQuery({
    queryKey: ["course", courseId],
    queryFn: () => api.getCourse(courseId),
    retry: false,
  });
  const outlineApproved = course?.status === "approved";
  const { diagramSrcs, chartSrcs, chartTables } = useDiagramMaps(rows);
  const [textEdit, setTextEdit] = useState<ContentRow | null>(null);
  const [quizEdit, setQuizEdit] = useState<ContentRow | null>(null);
  const [quizEditText, setQuizEditText] = useState({ question: "", options: "", answer: "", explanation: "" });
  const [flowView, setFlowView] = useState<ContentRow | null>(null);
  const [imgModal, setImgModal] = useState<ImageDraft | null>(null);
  const [imgPreview, setImgPreview] = useState<ContentRow | null>(null);
  const [flowModal, setFlowModal] = useState<FlowDraft | null>(null);
  const [mermaidModal, setMermaidModal] = useState<MermaidDraft | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["content", courseId] });
  const chapterOptions = useMemo(() => {
    const list = (plan?.nodes ?? []).map((n) => ({
      value: n.id,
      label: `${n.key} ${n.title}`,
    }));
    return [{ value: "", label: "（整门课）自动构思整体示意图" }, ...list];
  }, [plan]);
  const selectedChapter = useMemo(
    () => (plan?.nodes ?? []).find((n) => n.id === imgModal?.nodeId),
    [plan, imgModal?.nodeId],
  );
  const openImgModal = (draft: ImageDraft) => {
    setImgModal(draft);
    setImgPreview(null);
  };
  const closeImgModal = () => {
    if (imgPreview) requestAutoSync(courseId);
    setImgModal(null);
    setImgPreview(null);
  };
  const generate = useMutation({
    mutationFn: api.generateContent,
    onSuccess: async ({ job_id }) => {
      message.info("内容生成中，请稍候…");
      await new Promise((r) => setTimeout(r, 3000));
      const status = await api.jobStatus(job_id);
      if (status.status === "success") {
        message.success("内容生成完成");
        requestAutoSync(courseId);
      } else message.error(status.error || "生成失败");
      refresh();
    },
    onError: (e) => message.error(String(e)),
  });
  const quiz = useMutation({
    mutationFn: (v: { count: number; difficulty: string }) => api.generateQuiz({ course_id: courseId, ...v }),
    onSuccess: () => { message.success("习题已生成"); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const imgGen = useMutation({
    mutationFn: (d: ImageDraft) =>
      api.generateImage({ course_id: courseId, prompt: d.prompt, size: d.size, item_id: d.itemId, node_id: d.nodeId ?? "" }),
    onSuccess: (row) => { message.success("图片已生成，请预览确认"); setImgPreview(row); refresh(); },
    onError: (e) => message.error(String(e)),
  });
  const imgImport = useMutation({
    mutationFn: (f: File) => api.importImage(courseId, f),
    onSuccess: () => { message.success("已导入图片"); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const flowGen = useMutation({
    mutationFn: (d: FlowDraft) =>
      api.generateFlowchart({ course_id: courseId, description: d.description, kind: d.kind, item_id: d.itemId, node_id: d.nodeId ?? "" }),
    onSuccess: () => { message.success("流程图已生成"); setFlowModal(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const flowImport = useMutation({
    mutationFn: (d: MermaidDraft) =>
      api.importFlowchart({ course_id: courseId, title: d.title, mermaid: d.mermaid, item_id: d.itemId, node_id: d.nodeId ?? "" }),
    onSuccess: () => { message.success("已导入流程图"); setMermaidModal(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const quizSave = useMutation({
    mutationFn: (d: { id: string; body: Record<string, unknown> }) => api.updateQuiz(d.id, d.body),
    onSuccess: () => { message.success("已保存"); setQuizEdit(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const patchItem = useMutation({
    mutationFn: (d: { id: string; body: Record<string, unknown> }) => api.updateContent(d.id, { body: d.body }),
    onSuccess: () => { message.success("已保存"); setTextEdit(null); setQuizEdit(null); setFlowView(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const delItem = useMutation({
    mutationFn: (id: string) => api.deleteContent(id),
    onSuccess: () => { message.success("已删除"); refresh(); },
    onError: (e) => message.error(String(e)),
  });
  const texts = rows.filter((r) => r.kind === "text");
  const quizzes = rows.filter((r) => r.kind === "quiz");
  const images = rows.filter((r) => r.kind === "image");
  const flows = rows.filter((r) => r.kind === "flowchart");
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {!outlineApproved && (
        <Alert
          type="warning"
          showIcon
          message="大纲尚未通过"
          description="请先在「大纲规划」步骤点「大纲通过」锁定大纲，才能继续自动生成内容/图片/流程图；已生成的内容仍可编辑与删除。"
        />
      )}
      <SectionCard title="批量生成" size="small">
        <Form form={form} layout="inline" onFinish={(v) => generate.mutate({ course_id: courseId, ...v })}>
          <Form.Item name="tone" label="语气" initialValue="正式">
            <Select style={{ width: 120 }} options={["正式", "轻松", "幽默"].map((v) => ({ value: v, label: v }))} />
          </Form.Item>
          <Form.Item name="include_quiz" label="含习题" initialValue={true}>
            <Select style={{ width: 120 }} options={[{ value: true, label: "是" }, { value: false, label: "否" }]} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={generate.isPending}>生成文本+习题</Button>
          <Button onClick={() => quiz.mutate({ count: 5, difficulty: "medium" })} loading={quiz.isPending}>
            追加 5 道习题
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            再次生成会按知识点原地替换（旧版进历史），不会越堆越多。
          </Typography.Text>
        </Form>
      </SectionCard>
      {/* 图片 */}
      <SectionCard
        title={`图片（${images.length}）· 可重做 / 导入自己的图`}
        size="small"
        extra={
          <Space>
            <Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => openImgModal({ prompt: "", size: "1024x1024", nodeId: "" })}>
              生成图片
            </Button>
            <Upload accept="image/*" showUploadList={false} beforeUpload={(f) => { imgImport.mutate(f); return false; }}>
              <Button size="small" icon={<UploadOutlined />}>导入我的图片</Button>
            </Upload>
          </Space>
        }
      >
        {images.length === 0 ? (
          <Empty description="暂无图片，可生成或导入" />
        ) : (
          <List
            size="small"
            grid={{ gutter: 12, column: 3 }}
            dataSource={images}
            renderItem={(im) => (
              <List.Item>
                <SectionCard
                  size="small"
                  cover={<img alt={im.title} src={imgSrc(im)} style={{ maxHeight: 180, objectFit: "cover" }} />}
                  title={<Typography.Text ellipsis style={{ maxWidth: 180 }}>{im.title}</Typography.Text>}
                  actions={[
                    <a key="redo" onClick={() => openImgModal({ prompt: String(im.body.prompt ?? im.title), size: String(im.body.size ?? "1024x1024"), itemId: im.id, nodeId: im.node_id ?? "" })}>
                      <ReloadOutlined /> 重做
                    </a>,
                    <Popconfirm key="del" title="删除该图片？" onConfirm={() => delItem.mutate(im.id)}>
                      <a>删除</a>
                    </Popconfirm>,
                  ]}
                >
                  <Tag>{String(im.body.source ?? "generate")}</Tag>
                  {Boolean(im.body.prompt) && <Typography.Text type="secondary" style={{ fontSize: 12 }}>{String(im.body.prompt).slice(0, 40)}</Typography.Text>}
                </SectionCard>
              </List.Item>
            )}
          />
        )}
      </SectionCard>
      {/* 流程图 */}
      <SectionCard
        title={`流程图（${flows.length}）· 可重做 / 自绘导入 Mermaid`}
        size="small"
        extra={
          <Space>
            <Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => setFlowModal({ description: "", kind: "flow", nodeId: "" })}>
              生成流程图
            </Button>
            <Button size="small" icon={<UploadOutlined />} onClick={() => setMermaidModal({ title: "", mermaid: "", nodeId: "" })}>
              自绘/粘贴 Mermaid
            </Button>
          </Space>
        }
      >
        {flows.length === 0 ? (
          <Empty description="暂无流程图，可生成或导入自绘 Mermaid" />
        ) : (
          <List
            size="small"
            dataSource={flows}
            renderItem={(fc) => (
              <List.Item
                actions={[
                  <a key="pv" onClick={() => setFlowView(fc)}>查看源码</a>,
                  <a key="redo" onClick={() => setFlowModal({ description: String(fc.body.description ?? fc.title), kind: String(fc.body.kind ?? "flow"), itemId: fc.id, nodeId: fc.node_id ?? "" })}>
                    <ReloadOutlined /> 重做
                  </a>,
                  <a key="rep" onClick={() => setMermaidModal({ title: fc.title, mermaid: String(fc.body.mermaid ?? ""), itemId: fc.id, nodeId: fc.node_id ?? "" })}>粘贴替换</a>,
                  <Popconfirm key="del" title="删除该流程图？" onConfirm={() => delItem.mutate(fc.id)}>
                    <a>删除</a>
                  </Popconfirm>,
                ]}
              >
                <Tag color="blue">{fc.title}</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {String(fc.body.description ?? "") || String(fc.body.source ?? "")}
                </Typography.Text>
              </List.Item>
            )}
          />
        )}
      </SectionCard>
      {/* 讲解文本 */}
      <SectionCard title={`讲解文本（${texts.length}）· 可编辑 / 删除`} size="small">
        {texts.length === 0 ? (
          <Empty description="暂无文本内容" />
        ) : (
          <Collapse
            items={texts.map((t) => ({
              key: t.id,
              label: t.title,
              extra: (
                <Space size={4} onClick={(e) => e.stopPropagation()}>
                  <a onClick={() => setTextEdit(t)}>编辑</a>
                  <Popconfirm title="删除该文本？" onConfirm={() => delItem.mutate(t.id)}>
                    <a>删除</a>
                  </Popconfirm>
                </Space>
              ),
              children: (
                <div>
                  <Space size={4}>
                    {(t.body.keywords as string[] | undefined)?.map((k) => <Tag key={k}>{k}</Tag>)}
                  </Space>
                  <MdWithFlow
                    md={String(t.body.markdown ?? "")}
                    diagramSrcs={diagramSrcs}
                    chartSrcs={chartSrcs}
                    chartTables={chartTables}
                  />
                </div>
              ),
            }))}
          />
        )}
      </SectionCard>
      {/* 习题 */}
      <SectionCard title={`习题（${quizzes.length}）· 可编辑 / 删除`} size="small">
        <Collapse
          items={quizzes.map((q) => {
            const body = q.body as { question: string; options?: string[]; answer: string; difficulty: string; explanation?: string };
            return {
              key: q.id,
              label: `${body.question}（${body.difficulty}）`,
              extra: (
                <Space size={4} onClick={(e) => e.stopPropagation()}>
                  <a
                    onClick={() => {
                      setQuizEdit(q);
                      setQuizEditText({
                        question: body.question ?? "",
                        options: (body.options ?? []).join("\n"),
                        answer: body.answer ?? "",
                        explanation: body.explanation ?? "",
                      });
                    }}
                  >
                    编辑
                  </a>
                  <Popconfirm title="删除该习题？" onConfirm={() => api.deleteQuiz(q.id).then(() => { message.success("已删除"); refresh(); })}>
                    <a>删除</a>
                  </Popconfirm>
                </Space>
              ),
              children: (
                <div>
                  <p>选项：{body.options?.join(" / ") || "—"}</p>
                  {/* 任务 90：与分享页/预览页同一口径 —— 空值连标签一起不渲染 */}
                  {body.answer ? <p>答案：{body.answer}</p> : null}
                  {body.explanation ? <p>解析：{body.explanation}</p> : null}
                </div>
              ),
            };
          })}
        />
      </SectionCard>
      {/* 文本编辑弹窗 */}
      <Modal
        title={`编辑讲解：${textEdit?.title ?? ""}`}
        open={!!textEdit}
        onCancel={() => setTextEdit(null)}
        onOk={() => textEdit && patchItem.mutate({ id: textEdit.id, body: { markdown: String((document.getElementById("md-editor") as HTMLTextAreaElement)?.value ?? "") } })}
      >
        <Input.TextArea
          id="md-editor"
          rows={14}
          defaultValue={String(textEdit?.body.markdown ?? "")}
          placeholder="Markdown 正文"
        />
      </Modal>
      {/* 习题编辑弹窗 */}
      <Modal
        title="编辑习题"
        open={!!quizEdit}
        onCancel={() => setQuizEdit(null)}
        onOk={() =>
          quizEdit &&
          quizSave.mutate({
            id: quizEdit.id,
            body: {
              question: quizEditText.question,
              options: quizEditText.options.split("\n").map((s) => s.trim()).filter(Boolean),
              answer: quizEditText.answer,
              explanation: quizEditText.explanation,
            },
          })
        }
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input.TextArea rows={2} value={quizEditText.question} onChange={(e) => setQuizEditText({ ...quizEditText, question: e.target.value })} placeholder="题干（选择题把答案填选项序号 0/1/2…）" />
          <Input.TextArea rows={4} value={quizEditText.options} onChange={(e) => setQuizEditText({ ...quizEditText, options: e.target.value })} placeholder="选项，每行一个（非选择题留空）" />
          <Input value={quizEditText.answer} onChange={(e) => setQuizEditText({ ...quizEditText, answer: e.target.value })} placeholder="答案" />
          <Input.TextArea rows={2} value={quizEditText.explanation} onChange={(e) => setQuizEditText({ ...quizEditText, explanation: e.target.value })} placeholder="解析" />
        </Space>
      </Modal>
      {/* 流程图源码查看 */}
      <Modal title={`流程图源码：${flowView?.title ?? ""}`} open={!!flowView} footer={null} width={760} onCancel={() => setFlowView(null)}>
        <pre style={{ whiteSpace: "pre-wrap", maxHeight: 460, overflow: "auto" }}>
          {String(flowView?.body.mermaid ?? "")}
        </pre>
      </Modal>
      {/* 图片生成/重做 */}
      <Modal
        title={imgModal?.itemId ? "重做图片（原地替换）" : "生成图片"}
        open={!!imgModal}
        onCancel={closeImgModal}
        footer={null}
        width={680}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Space>
            <Typography.Text style={{ minWidth: 56 }}>位图配图章节</Typography.Text>
            <Select
              style={{ flex: 1, minWidth: 260 }}
              value={imgModal?.nodeId ?? ""}
              onChange={(nodeId) => setImgModal({ ...(imgModal as ImageDraft), nodeId })}
              options={chapterOptions}
              placeholder="选择章节（可选）"
            />
          </Space>
          <Input.TextArea
            rows={2}
            value={imgModal?.prompt ?? ""}
            onChange={(e) => setImgModal({ ...(imgModal as ImageDraft), prompt: e.target.value })}
            placeholder="补充画面细节（选填；留空将自动按所选章节或整门课大纲构图）"
          />
          <Space>
            <Typography.Text>尺寸</Typography.Text>
            <Select
              style={{ width: 200 }}
              value={imgModal?.size}
              onChange={(size) => setImgModal({ ...(imgModal as ImageDraft), size })}
              options={["512x512", "1024x1024", "1024x1792"].map((v) => ({ value: v, label: v }))}
            />
          </Space>
          {imgGen.isPending ? (
            <div style={{ textAlign: "center", padding: "24px 0" }}>
              <Spin size="large" />
              <div style={{ marginTop: 12, color: "#64748b" }}>正在生成图片（约 10~30 秒），请稍候…</div>
            </div>
          ) : imgPreview ? (
            <div style={{ textAlign: "center" }}>
              <img
                src={imgSrc(imgPreview)}
                alt={imgPreview.title}
                style={{ maxWidth: "100%", maxHeight: 360, borderRadius: 8, boxShadow: "0 2px 12px rgba(0,0,0,.14)" }}
              />
              <Space style={{ marginTop: 16 }} wrap>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => imgModal && imgPreview && imgGen.mutate({ ...imgModal, itemId: imgModal.itemId ?? imgPreview.id })}
                >
                  不满意 · 换一张
                </Button>
                <Button type="primary" onClick={closeImgModal}>满意 · 完成</Button>
              </Space>
              <Typography.Text type="secondary" style={{ display: "block", marginTop: 8, fontSize: 12 }}>
                换一张会原地替换上一版（旧版自动进历史）；图片已存入课程，点“完成”即同步到 Obsidian 笔记。
              </Typography.Text>
            </div>
          ) : (
            <div>
              <Typography.Text type="secondary" style={{ display: "block", marginBottom: 12, fontSize: 12 }}>
                {selectedChapter
                  ? `将自动按「${selectedChapter.key} ${selectedChapter.title}」的学习目标与知识点构图；也可在上方补充想要的画面细节。`
                  : "提示词留空时，将自动按整门课大纲（章节脉络）构思整体示意图；也可在上方补充想要的画面细节。"}
              </Typography.Text>
              <Button type="primary" icon={<PlusOutlined />} onClick={() => imgModal && imgGen.mutate(imgModal)}>
                开始生成
              </Button>
            </div>
          )}
        </Space>
      </Modal>
      {/* 流程图生成/重做 */}
      <Modal
        title={flowModal?.itemId ? "重做流程图（原地替换）" : "生成流程图"}
        open={!!flowModal}
        onCancel={() => setFlowModal(null)}
        onOk={() => flowModal && flowGen.mutate(flowModal)}
        confirmLoading={flowGen.isPending}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Select
            style={{ width: "100%" }}
            value={flowModal?.nodeId ?? ""}
            placeholder="归属章节/知识点（选填；不选为整课级流程图）"
            options={chapterOptions}
            onChange={(nodeId) => setFlowModal({ ...(flowModal as FlowDraft), nodeId })}
          />
          <Input.TextArea
            rows={2}
            value={flowModal?.description ?? ""}
            onChange={(e) => setFlowModal({ ...(flowModal as FlowDraft), description: e.target.value })}
            placeholder="自然语言描述流程，如：光合作用：吸收光能→水分解→产生氧气→合成有机物"
          />
          <Select
            style={{ width: 200 }}
            value={flowModal?.kind}
            onChange={(kind) => setFlowModal({ ...(flowModal as FlowDraft), kind })}
            options={["flow", "loop", "branch", "hierarchy"].map((v) => ({ value: v, label: v }))}
          />
        </Space>
      </Modal>
      {/* 自绘 Mermaid 导入 */}
      <Modal
        title={mermaidModal?.itemId ? "粘贴替换流程图" : "导入自绘 Mermaid"}
        open={!!mermaidModal}
        onCancel={() => setMermaidModal(null)}
        onOk={() => mermaidModal && flowImport.mutate(mermaidModal)}
        confirmLoading={flowImport.isPending}
        width={760}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Select
            style={{ width: "100%" }}
            value={mermaidModal?.nodeId ?? ""}
            placeholder="归属章节/知识点（选填；不选为整课级流程图）"
            options={chapterOptions}
            onChange={(nodeId) => setMermaidModal({ ...(mermaidModal as MermaidDraft), nodeId })}
          />
          <Input
            value={mermaidModal?.title ?? ""}
            onChange={(e) => setMermaidModal({ ...(mermaidModal as MermaidDraft), title: e.target.value })}
            placeholder="标题（必填），如：水循环流程图"
          />
          <Input.TextArea
            rows={8}
            value={mermaidModal?.mermaid ?? ""}
            onChange={(e) => setMermaidModal({ ...(mermaidModal as MermaidDraft), mermaid: e.target.value })}
            placeholder={"flowchart TD\n  A([开始]) --> B[处理]\n  B --> C([结束])"}
          />
        </Space>
      </Modal>
    </Space>
  );
}
