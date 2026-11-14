import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, Button, Card, Collapse, Input, Select, Space, Tag, Tree, Typography,
} from "antd";
import type { TreeDataNode } from "antd";
import { DownloadOutlined, FileZipOutlined, SaveOutlined, FolderOpenOutlined } from "@ant-design/icons";
import { api } from "../../api";
import { triggerDownload } from "../../downloadUtils";
import { preservedFileText } from "../../preserved";
import ConsistencyCheck from "../ConsistencyCheck";
import ConceptPanel from "../ConceptPanel";
import type { PreservedEntry } from "../../types";

function fileTreeFromPaths(files: string[], rootTitle: string): TreeDataNode[] {
  const root: TreeDataNode = { key: "__root", title: rootTitle, children: [] };
  const map = new Map<string, TreeDataNode>([[root.key as string, root]]);
  for (const f of files) {
    const parts = f.split("/").filter(Boolean);
    let parentKey = root.key as string;
    parts.forEach((part, idx) => {
      const isLeaf = idx === parts.length - 1;
      const key = `${parentKey}/${part}`;
      let node = map.get(key);
      if (!node) {
        node = { key, title: part, isLeaf, children: [] };
        map.set(key, node);
        map.get(parentKey)?.children?.push(node);
      } else if (isLeaf) {
        node.isLeaf = true;
      }
      parentKey = key;
    });
  }
  const sortNode = (n: TreeDataNode): TreeDataNode => {
    const kids = (n.children ?? []).map(sortNode).sort((a, b) => {
      const aDir = (a.children?.length ?? 0) > 0 ? 0 : 1;
      const bDir = (b.children?.length ?? 0) > 0 ? 0 : 1;
      return aDir - bDir || String(a.title).localeCompare(String(b.title), "zh");
    });
    return { ...n, children: kids };
  };
  return [sortNode(root)];
}

export default function NoteStep({ courseId }: { courseId: string }) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  const { data: kb } = useQuery({ queryKey: ["kb", courseId], queryFn: () => api.kbInfo(courseId) });
  const { data: plan } = useQuery({ queryKey: ["plan", courseId], queryFn: () => api.getPlan(courseId), retry: false });
  const [vaultRoot, setVaultRoot] = useState<string>("");
  const [outputRel, setOutputRel] = useState<string>("");
  const [zipNodes, setZipNodes] = useState<string[]>([]);
  const [result, setResult] = useState<{
    files: string[]; preserved: PreservedEntry[]; warnings?: string[]; charts: string[]; outputRel?: string;
  } | null>(null);
  const planNodes = plan?.nodes ?? [];
  const nodeOptions = planNodes.map((n) => ({ value: n.id, label: `${n.key} ${n.title}` }));

  const effectiveRoot = (vaultRoot || kb?.vault.vault_root || "").trim();
  const effectiveRel = (outputRel || (kb?.kb.output_rel as string | undefined) || "").trim();

  const bind = useMutation({
    mutationFn: () =>
      api.kbBind(courseId, {
        vault_root: vaultRoot.trim() ? vaultRoot.trim() : null,
        output_rel: outputRel.trim() ? outputRel.trim() : null,
      }),
    onSuccess: () => { message.success("已保存导出目录"); qc.invalidateQueries({ queryKey: ["kb", courseId] }); },
    onError: (e) => message.error(String(e)),
  });

  const doExport = useMutation({
    mutationFn: (force: boolean) => api.kbExport(courseId, force),
    onSuccess: (res) => {
      const warned = res.warnings?.length ? `（${res.warnings.length} 条提示）` : "";
      message.success(
        res.preserved.length
          ? `已保存（保留了你手动改过的 ${res.preserved.length} 个文件）`
          : `已生成笔记系统：${res.output_rel}（${res.files.length} 个文件${warned}）`,
      );
      setResult({
        files: res.files, preserved: res.preserved, warnings: res.warnings, charts: res.charts,
        outputRel: res.output_rel,
      });
      qc.invalidateQueries({ queryKey: ["kb", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });

  const doZip = useMutation({
    mutationFn: (scope: "all" | "nodes") =>
      api.exportNotesZip({
        course_id: courseId,
        scope,
        node_ids: scope === "nodes" ? zipNodes : [],
      }),
    onSuccess: (res) => {
      message.success(res.scope === "all" ? "整课笔记已打包，开始下载…" : `已打包 ${zipNodes.length} 个知识点，开始下载…`);
      triggerDownload(res.url, res.file);
    },
    onError: (e) => message.error(String(e)),
  });
  const outRel = result?.outputRel?.replace(/\/+$/, "") ?? "";
  const treeFiles = outRel
    ? (result?.files.map((f) => f.replace(`${outRel}/`, "")) ?? [])
    : (result?.files ?? []);

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert
        type="success"
        showIcon
        message="第 5 步 · 存入 Obsidian 笔记"
        description="一键生成整套 Obsidian 笔记：00-课程概览 = 目录 MOC；每章一个目录，每个知识点一页（优先完整讲义，无讲义自动拼接正文 / 概念图 / 数据图 / 流程图）；图片在章节 assets/、习题在 习题集/、可编辑图在 图表库/。网页端是唯一编辑源，再次导出不会覆盖你手动改过的文件。"
      />

      <ConceptPanel courseId={courseId} title="概念库（原子笔记 · 一概念一篇 + 双链）" />

      <Card size="small" title="导出目录">
        <Space direction="vertical" style={{ width: "100%" }}>
          <Space wrap style={{ width: "100%" }}>
            {/* 任务 48t：antd 已废弃 Input 的 addonBefore（控制台会警告），按提示换成 Space.Compact，
                图标块的外观与原来的 addon 一致。 */}
            <Space.Compact style={{ width: 420 }}>
              <Button icon={<FolderOpenOutlined />} disabled />
              <Input
                placeholder="Obsidian vault 根目录，如 D:\CourseGenie知识库"
                value={vaultRoot}
                onChange={(e) => setVaultRoot(e.target.value)}
              />
            </Space.Compact>
            <Input
              style={{ width: 320 }}
              placeholder="导出子目录，如 课程/光合作用（默认 课程/<课程名>）"
              value={outputRel}
              onChange={(e) => setOutputRel(e.target.value)}
            />
            <Button onClick={() => bind.mutate()} loading={bind.isPending}>保存目录</Button>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            当前目标：{effectiveRoot || "（未绑定 → 应用默认目录 storage/kb）"}
            {effectiveRel ? ` ｜ 导出到：${effectiveRel}` : ""}
          </Typography.Text>
        </Space>
      </Card>

      <Card size="small" title="一致性校验（网页 vs 笔记）">
        <ConsistencyCheck courseId={courseId} />
      </Card>

      <Card size="small" title="保存到笔记">
        <Space wrap>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            size="large"
            loading={doExport.isPending}
            onClick={() => doExport.mutate(false)}
          >
            保存到 Obsidian 笔记
          </Button>
          <Button disabled={doExport.isPending} onClick={() => doExport.mutate(true)}>
            强制覆盖（含手改文件）
          </Button>
          {result && (
            <Tag color={result.preserved.length ? "orange" : "green"}>
              {result.preserved.length ? `保留手改 ${result.preserved.length} 个` : "全部按最新导出"}
            </Tag>
          )}
        </Space>
        {result?.warnings?.length ? (
          <Alert style={{ marginTop: 12 }} type="warning" showIcon message="提示" description={result.warnings.join("；")} />
        ) : null}
      </Card>

      <Card size="small" title="打包下载（ZIP）">
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Space wrap>
            <Button
              type="primary"
              icon={<FileZipOutlined />}
              size="large"
              loading={doZip.isPending}
              onClick={() => doZip.mutate("all")}
            >
              导出完整课程 ZIP
            </Button>
            <Select
              mode="multiple"
              style={{ minWidth: 340 }}
              placeholder="选择要打包的知识点（可多选）"
              options={nodeOptions}
              value={zipNodes}
              onChange={(v) => setZipNodes(v as string[])}
              maxTagCount="responsive"
              allowClear
            />
            <Button
              icon={<DownloadOutlined />}
              disabled={zipNodes.length === 0 || doZip.isPending}
              onClick={() => doZip.mutate("nodes")}
            >
              导出所选知识点 ZIP
            </Button>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            ZIP 内含：00-课程概览、各章知识点页（讲义优先）、章节 assets 图片、习题集、图表库；按知识点导出会自动带上依赖的概念图 / 数据图与习题页。
          </Typography.Text>
        </Space>
      </Card>

      {result && (
        <Card size="small" title={`导出结果：${result.files.length} 个文件 · ${result.charts.length} 张图`}>
          <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginTop: 0 }}>
            新目录结构预览（按知识点成页）：
          </Typography.Paragraph>
          <div style={{ background: "#fafafa", border: "1px solid #f0f0f0", borderRadius: 8, padding: "8px 12px", marginBottom: 12 }}>
            <Tree
              treeData={fileTreeFromPaths(treeFiles, result.outputRel ?? "课程笔记")}
              defaultExpandAll
              showLine
              selectable={false}
              style={{ background: "transparent", fontSize: 13 }}
            />
          </div>
          <Collapse
            size="small"
            items={[
              {
                key: "files",
                label: `本次写入/更新 ${result.files.length} 个`,
                children: (
                  <div style={{ maxHeight: 320, overflow: "auto" }}>
                    {result.files.map((f) => <div key={f} style={{ fontSize: 12 }}>{f}</div>)}
                  </div>
                ),
              },
              ...(result.preserved.length
                ? [{
                    key: "preserved",
                    label: `检测到你手动改过、已跳过覆盖 ${result.preserved.length} 个`,
                    children: (
                      <div style={{ maxHeight: 200, overflow: "auto" }}>
                        {result.preserved.map((f) => <div key={f.file} style={{ fontSize: 12 }}>{preservedFileText(f)}</div>)}
                      </div>
                    ),
                  }]
                : []),
            ]}
          />
        </Card>
      )}

      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        导出后可在 Obsidian 中打开笔记检查；需要改课程请回到本网页修改后重新点「保存到 Obsidian 笔记」。
      </Typography.Text>
    </Space>
  );
}
