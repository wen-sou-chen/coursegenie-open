import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import SectionCard from "./ui/SectionCard";
import { Button, Input, Popconfirm, Space, Table, message } from "antd";
import { api } from "../api";
import { formatLocalTime } from "../lib/datetime";
export default function VersionsPanel({ courseId }: { courseId: string }) {
  const qc = useQueryClient();
  // 任务 48y：备注要能填（原来 snapshot 不传 note，表格里那列永远是空）
  const [note, setNote] = useState("");
  const { data: versions = [] } = useQuery({ queryKey: ["versions", courseId], queryFn: () => api.versions(courseId) });
  const snapshot = useMutation({
    mutationFn: () => api.snapshot(courseId, note),
    onSuccess: () => {
      message.success("已保存版本快照");
      setNote("");
      qc.invalidateQueries({ queryKey: ["versions", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });
  const restore = useMutation({
    mutationFn: (versionId: string) => api.restore(courseId, versionId),
    onSuccess: () => {
      message.success("已回滚");
      qc.invalidateQueries({ queryKey: ["versions", courseId] });
      qc.invalidateQueries({ queryKey: ["plan", courseId] });
      qc.invalidateQueries({ queryKey: ["content", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });
  return (
    <SectionCard
      title="版本历史"
      extra={
        <Space>
          <Input
            placeholder="备注（可选）"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            allowClear
            style={{ width: 200 }}
          />
          <Button type="primary" onClick={() => snapshot.mutate()} loading={snapshot.isPending}>保存快照</Button>
        </Space>
      }
    >
      <Table
        rowKey="id"
        dataSource={versions}
        pagination={false}
        columns={[
          { title: "版本", dataIndex: "version_no", width: 90 },
          { title: "备注", dataIndex: "note" },
          /* 任务 81 §B2：时间统一成本地时间 + 人类可读（原先直接渲染 ISO：2026-09-21T06:24:46.708384）。 */
          { title: "时间", dataIndex: "created_at", width: 150,
            render: (at: string) => formatLocalTime(at) },
          {
            title: "操作",
            width: 120,
            render: (_, r) => (
              <Popconfirm title="确认回滚到该版本？" onConfirm={() => restore.mutate(r.id)}>
                <Button size="small" danger loading={restore.isPending}>回滚</Button>
              </Popconfirm>
            ),
          },
        ]}
      />
    </SectionCard>
  );
}
