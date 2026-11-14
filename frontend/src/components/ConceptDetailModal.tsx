import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Divider, Input, Modal, Space, Tag, Tooltip, Typography, message } from "antd";
import { SaveOutlined } from "@ant-design/icons";
import { api } from "../api";
import { requestAutoSync } from "../syncNotes";
import type { ConceptRow } from "../types";

const RELATION_FIELDS: { key: "broader" | "narrower" | "related"; label: string; hint: string }[] = [
  { key: "broader", label: "上位概念", hint: "本概念所属的更大概括，如「乳酸菌」" },
  { key: "narrower", label: "下位概念", hint: "本概念包含的具体种类" },
  { key: "related", label: "相关概念", hint: "平级的关联概念（不强指方向）" },
];

function toLines(values?: string[]): string {
  return (values ?? []).join("\n");
}

function fromLines(text: string): string[] {
  return text.split(/[\n,，、;；]+/).map((line) => line.trim()).filter(Boolean);
}

/**
 * 概念详情 / 编辑器（网页端是唯一编辑源）：
 * 打开即可看定义、要点、常见误区、别名、关系与来源出处，并可直接改。
 * 保存走 PATCH /api/concept/{id}（后端会标记「人工编辑」，之后重抽不再覆盖），
 * 保存成功后自动同步到 Obsidian 笔记（概念库/<概念名>.md 与 _图谱）。
 */
export default function ConceptDetailModal({
  courseId, concept, onClose,
}: {
  courseId: string;
  concept: ConceptRow | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [definition, setDefinition] = useState("");
  const [points, setPoints] = useState("");
  const [pitfalls, setPitfalls] = useState("");
  const [aliases, setAliases] = useState("");
  const [relations, setRelations] = useState({ broader: "", narrower: "", related: "" });

  useEffect(() => {
    if (!concept) return;
    setDefinition(concept.body?.definition ?? "");
    setPoints(toLines(concept.body?.points));
    setPitfalls(toLines(concept.body?.pitfalls));
    setAliases((concept.aliases ?? []).join("、"));
    setRelations({
      broader: toLines(concept.body?.relations?.broader),
      narrower: toLines(concept.body?.relations?.narrower),
      related: toLines(concept.body?.relations?.related),
    });
  }, [concept]);

  const dirty = useMemo(() => {
    if (!concept) return false;
    return definition !== (concept.body?.definition ?? "")
      || points !== toLines(concept.body?.points)
      || pitfalls !== toLines(concept.body?.pitfalls)
      || aliases !== (concept.aliases ?? []).join("、")
      || RELATION_FIELDS.some(({ key }) => relations[key] !== toLines(concept.body?.relations?.[key]));
  }, [concept, definition, points, pitfalls, aliases, relations]);

  const save = useMutation({
    mutationFn: () => api.updateConcept(concept!.id, {
      definition: definition.trim(),
      points: fromLines(points),
      pitfalls: fromLines(pitfalls),
      aliases: fromLines(aliases),
      relations: {
        broader: fromLines(relations.broader),
        narrower: fromLines(relations.narrower),
        related: fromLines(relations.related),
      },
    }),
    onSuccess: () => {
      message.success("已保存（标记为人工编辑，重新抽取不会覆盖），正在同步到笔记…");
      qc.invalidateQueries({ queryKey: ["concepts", courseId] });
      requestAutoSync(courseId);
      onClose();
    },
    onError: (e) => message.error(`保存失败：${String(e)}`),
  });

  if (!concept) return null;
  const sources = concept.body?.sources ?? [];

  return (
    <Modal
      open
      width={720}
      title={(
        <Space size={8} wrap>
          <span>概念详情 · {concept.name}</span>
          {concept.edited ? <Tag color="orange">人工编辑</Tag> : <Tag>自动抽取</Tag>}
          {concept.aliases?.length ? <Tag color="blue">别名 {concept.aliases.join("、")}</Tag> : null}
        </Space>
      )}
      onCancel={onClose}
      footer={(
        <Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {dirty ? "有未保存的修改" : "内容与当前一致"}
          </Typography.Text>
          <Button onClick={onClose}>取消</Button>
          <Tooltip title={dirty ? "保存并同步到 Obsidian 笔记" : "没改内容就不用保存（避免误标为人工编辑）"}>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              disabled={!dirty}
              loading={save.isPending}
              onClick={() => save.mutate()}
            >
              保存并同步到笔记
            </Button>
          </Tooltip>
        </Space>
      )}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Space size={6} wrap>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            出现章节：
          </Typography.Text>
          {concept.chapters?.length
            ? concept.chapters.map((key) => <Tag key={key} color="blue">{key}</Tag>)
            : <Typography.Text type="secondary">—</Typography.Text>}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            业务 id：{concept.concept_id || "—"}
          </Typography.Text>
        </Space>

        <div>
          <Typography.Text strong>一句话定义</Typography.Text>
          <Input.TextArea
            rows={2}
            value={definition}
            onChange={(e) => setDefinition(e.target.value)}
            placeholder="用一句话说清它是什么（学生第一眼就看这句）"
          />
        </div>

        <div>
          <Typography.Text strong>要点</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>（每行一条，3~5 条最好）</Typography.Text>
          <Input.TextArea
            rows={4}
            value={points}
            onChange={(e) => setPoints(e.target.value)}
            placeholder={"降低面团 pH 值\n重塑面筋骨架\n赋予面团微酸风味"}
          />
        </div>

        <div>
          <Typography.Text strong>常见误区</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>（每行一条，没有可留空）</Typography.Text>
          <Input.TextArea
            rows={2}
            value={pitfalls}
            onChange={(e) => setPitfalls(e.target.value)}
            placeholder="学生最容易搞错的地方，写一条就够用"
          />
        </div>

        <div>
          <Typography.Text strong>别名</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            （多个用「、」或逗号分隔；讲义里写别名也算术语一致）
          </Typography.Text>
          <Input
            value={aliases}
            onChange={(e) => setAliases(e.target.value)}
            placeholder="如：LAB、乳酸杆菌"
          />
        </div>

        <Divider style={{ margin: "4px 0" }} />

        <div>
          <Typography.Text strong>关系</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            （每行一个概念名；名字要跟概念库里的名字一致，否则图谱里只列出、不画线）
          </Typography.Text>
          <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 8 }}>
            {RELATION_FIELDS.map(({ key, label, hint }) => (
              <div key={key}>
                <Tooltip title={hint}>
                  <Typography.Text style={{ fontSize: 13 }}>{label}</Typography.Text>
                </Tooltip>
                <Input.TextArea
                  rows={key === "related" ? 2 : 1}
                  value={relations[key]}
                  onChange={(e) => setRelations((prev) => ({ ...prev, [key]: e.target.value }))}
                  placeholder="每行一个概念名"
                />
              </div>
            ))}
          </Space>
        </div>

        {!!sources.length && (
          <>
            <Divider style={{ margin: "4px 0" }} />
            <div>
              <Typography.Text strong>来源出处</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>（抽取时引用的讲义/素材原文）</Typography.Text>
              {sources.map((item, index) => (
                <div key={index} style={{ fontSize: 12, marginTop: 4 }}>
                  <Tag>{item.kind}</Tag>
                  {item.ref}
                  {item.quote ? `：${item.quote}` : ""}
                </div>
              ))}
            </div>
          </>
        )}

        <Alert
          type="info"
          showIcon
          message="这里改完就是最终版本"
          description="网页端是唯一编辑源：保存后会自动写回 Obsidian（概念库/<概念名>.md 与 _图谱），笔记端手改会在下次导出时被覆盖。"
        />
      </Space>
    </Modal>
  );
}
