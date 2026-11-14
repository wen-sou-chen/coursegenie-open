import { useState } from "react";
import SectionCard from "./ui/SectionCard";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, Button, Empty, Input, List, Modal, Popconfirm, Space, Tag, Typography, Upload, message,
} from "antd";
import {
  ExportOutlined, FolderOpenOutlined, FileAddOutlined, SaveOutlined, DeleteOutlined,
  AppstoreAddOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { requestAutoSync } from "../syncNotes";
import { preservedFileText } from "../preserved";
import type { MaterialRow, PreservedEntry } from "../types";
function vaultName(root: string): string {
  return root.split(/[\\/]/).filter(Boolean).pop() ?? root;
}
export default function KbPanel({ courseId }: { courseId: string }) {
  const qc = useQueryClient();
  const [override, setOverride] = useState<string | undefined>(undefined);
  const [outputRel, setOutputRel] = useState<string | undefined>(undefined);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteTitle, setPasteTitle] = useState("");
  const [pasteText, setPasteText] = useState("");
  const [preview, setPreview] = useState<MaterialRow | null>(null);
  const [result, setResult] = useState<{ files: string[]; preserved: PreservedEntry[]; warnings?: string[] } | null>(null);
  const { data: kb, refetch: refetchKb } = useQuery({
    queryKey: ["kb", courseId],
    queryFn: () => api.kbInfo(courseId),
  });
  const { data: materials = [], refetch: refetchMats } = useQuery({
    queryKey: ["materials", courseId],
    queryFn: () => api.listMaterials(courseId),
  });
  const effectiveRoot = kb?.vault.vault_root ?? "";
  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ["kb", courseId] });
    qc.invalidateQueries({ queryKey: ["materials", courseId] });
  };
  const bind = useMutation({
    mutationFn: () =>
      api.kbBind(courseId, {
        vault_root: override?.trim() ? override.trim() : null,
        output_rel: outputRel?.trim() ? outputRel.trim() : null,
      }),
    onSuccess: () => { message.success("已保存导出目录"); refetchKb(); },
    onError: (e) => message.error(String(e)),
  });
  const upload = useMutation({
    mutationFn: (f: File) => api.uploadMaterial(courseId, f),
    onSuccess: () => { message.success("已上传并提取素材"); refetchMats(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const paste = useMutation({
    mutationFn: () => api.pasteMaterial({ course_id: courseId, title: pasteTitle, text: pasteText }),
    onSuccess: () => { message.success("已加入素材"); setPasteOpen(false); setPasteTitle(""); setPasteText(""); refetchMats(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const del = useMutation({
    mutationFn: (id: string) => api.deleteMaterial(id),
    onSuccess: () => { message.success("已删除素材"); refetchMats(); },
    onError: (e) => message.error(String(e)),
  });
  const styleLib = useMutation({
    mutationFn: () => api.exportStyleLibrary(courseId),
    onSuccess: (res) =>
      message.success(
        `已导出风格库：${res.file}（${res.stats.items} 个条目 / ${res.stats.elements} 个元素，主题 ${res.stats.theme_name}）`,
      ),
    onError: (e: Error) => message.error(`导出风格库失败：${e.message}`),
  });
  const doExport = useMutation({
    mutationFn: (force: boolean) => api.kbExport(courseId, force),
    onSuccess: (res) => {
      const warned = res.warnings?.length ? `（${res.warnings.length} 条提示）` : "";
      message.success(res.preserved.length
        ? `已保存（保留了你手动改过的 ${res.preserved.length} 个文件）`
        : `已生成笔记系统：${res.output_rel}（${res.files.length} 个文件${warned}）`);
      setResult({ files: res.files, preserved: res.preserved, warnings: res.warnings });
      refetchKb();
    },
    onError: (e) => message.error(String(e)),
  });
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="工作方式"
        description="教师只需提供原始资料（Word / PDF / PPT / 文本均可），CourseGenie 会自动生成这门课的整套笔记系统（课程概览 + 章节笔记 + 素材库 + 03图表库）。修改课程请在本网页操作（网页端是唯一编辑源）；导出的笔记是成果快照，再次导出不会覆盖你手动改过的文件。"
      />
      {/* 素材 */}
      <SectionCard
        title={`原始资料（${materials.length}）· 生成大纲/内容时会自动作为依据`}
        size="small"
        extra={
          <Space>
            <Upload
              accept=".md,.txt,.csv,.markdown,.docx,.pdf,.pptx,.png,.jpg,.jpeg,.webp,.mp4,.mov,.mkv,.webm,.avi,.m4v,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus"
              showUploadList={false}
              multiple
              beforeUpload={(f) => { upload.mutate(f); return false; }}
            >
              <Button size="small" type="primary" icon={<FileAddOutlined />}>上传资料</Button>
            </Upload>
            <Button size="small" onClick={() => setPasteOpen(true)}>粘贴文本</Button>
          </Space>
        }
      >
        {materials.length === 0 ? (
          <Empty description="还没有素材。上传你的备课文档（如 .docx/.pdf/.pptx/.md），或直接粘贴文字。" />
        ) : (
          <List
            size="small"
            dataSource={materials}
            renderItem={(m) => (
              <List.Item
                actions={[
                  <a key="pv" onClick={() => setPreview(m)}>预览</a>,
                  <Popconfirm key="del" title="删除该素材？" onConfirm={() => del.mutate(m.id)}>
                    <a><DeleteOutlined /> 删除</a>
                  </Popconfirm>,
                ]}
              >
                <Space>
                  <Tag color={m.kind === "text" ? "green" : m.kind === "image" ? "purple"
                    : m.kind === "video" ? "geekblue" : m.kind === "audio" ? "cyan" : "blue"}>
                    {m.kind === "text" ? "粘贴" : (m.ext || "文件")}
                  </Tag>
                  <Typography.Text strong>{m.title}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>{m.chars} 字{m.original ? ` · ${m.original}` : ""}</Typography.Text>
                </Space>
              </List.Item>
            )}
          />
        )}
      </SectionCard>
      {/* 笔记系统导出 */}
      <SectionCard title="自动生成笔记系统" size="small">
        <Space direction="vertical" style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="导出到哪？"
            description="默认导出到本机配置的 Obsidian 知识库（OBSIDIAN_VAULT_ROOT）；生成大纲/内容/图片/流程图后会自动同步到这里，也可随时点页面顶部「保存到 Obsidian 笔记」。没有 vault 也不影响——生成的就是普通 Markdown 文件夹。"
          />
          <Space wrap>
            <Typography.Text type="secondary">目标根目录（vault）：</Typography.Text>
            <Input
              style={{ width: 360 }}
              placeholder={effectiveRoot || "留空使用默认目录"}
              defaultValue={kb?.kb.vault_root}
              onChange={(e) => setOverride(e.target.value)}
            />
            <Typography.Text type="secondary">导出子目录：</Typography.Text>
            <Input
              style={{ width: 260 }}
              placeholder="课程/光合作用（默认 课程/<课程名>）"
              defaultValue={kb?.kb.output_rel}
              onChange={(e) => setOutputRel(e.target.value)}
            />
            <Button icon={<SaveOutlined />} loading={bind.isPending} onClick={() => bind.mutate()}>保存</Button>
          </Space>
          <Space wrap>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              当前目录：{effectiveRoot || "（未配置，将使用默认 storage/kb）"}
            </Typography.Text>
            {effectiveRoot && (
              <a
                href={`obsidian://open?vault=${encodeURIComponent(vaultName(effectiveRoot))}`}
                target="_blank"
                rel="noreferrer"
              >
                <FolderOpenOutlined /> 用 Obsidian 打开
              </a>
            )}
          </Space>
          <Space>
            <Button type="primary" icon={<ExportOutlined />} loading={doExport.isPending} onClick={() => doExport.mutate(false)}>
              生成笔记系统
            </Button>
          <Popconfirm title="强制覆盖你手动改过的笔记（不推荐）？" onConfirm={() => doExport.mutate(true)}>
              <Button danger size="small" loading={doExport.isPending}>强制覆盖</Button>
            </Popconfirm>
            <Button
              icon={<AppstoreAddOutlined />}
              loading={styleLib.isPending}
              onClick={() => styleLib.mutate()}
            >
              导出风格库（.excalidrawlib）
            </Button>
          </Space>
          {result && (
            <div>
              {result.preserved.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message={`有 ${result.preserved.length} 个文件未更新（已跳过覆盖，网页与笔记可能不一致）`}
                  description={result.preserved.slice(0, 8).map((f) => (
                    <div key={f.file} style={{ fontSize: 12, fontFamily: "monospace" }}>{preservedFileText(f)}</div>
                  ))}
                />
              )}
              {result.warnings && result.warnings.length > 0 && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message="提示"
                  description={result.warnings.slice(0, 5).join("；")}
                />
              )}
              <Typography.Paragraph style={{ marginBottom: 0 }}>
                {result.files.slice(0, 15).map((f) => (
                  <div key={f} style={{ fontSize: 12, fontFamily: "monospace" }}>{f}</div>
                ))}
                {result.files.length > 15 && <div style={{ fontSize: 12 }}>…共 {result.files.length} 个文件</div>}
              </Typography.Paragraph>
            </div>
          )}
        </Space>
      </SectionCard>
      {/* 粘贴素材 */}
      <Modal
        title="粘贴原始资料"
        open={pasteOpen}
        onCancel={() => setPasteOpen(false)}
        onOk={() => paste.mutate()}
        confirmLoading={paste.isPending}
        width={680}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input value={pasteTitle} onChange={(e) => setPasteTitle(e.target.value)} placeholder="资料标题，如：光合作用备课笔记" />
          <Input.TextArea rows={10} value={pasteText} onChange={(e) => setPasteText(e.target.value)} placeholder="把文档/笔记正文粘贴到这里…" />
        </Space>
      </Modal>
      {/* 素材预览 */}
      <Modal title={preview?.title} open={!!preview} footer={null} width={720} onCancel={() => setPreview(null)}>
        {preview && (
          <pre style={{ whiteSpace: "pre-wrap", maxHeight: 480, overflow: "auto" }}>
            {preview.preview || "（预览仅显示前 200 字，完整内容已用于生成）"}
          </pre>
        )}
      </Modal>
    </Space>
  );
}
