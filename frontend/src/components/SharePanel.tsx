/** 分享面板（M5a）：放在课程详情「高级工具」里，创建/撤销/删除只读分享链接。 */
import { useState } from "react";
import {
  Alert, Button, Card, Empty, Input, InputNumber, Popconfirm, Space, Table, Tag, Typography, message,
} from "antd";
import { CopyOutlined, DeleteOutlined, LinkOutlined, StopOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnsType } from "antd/es/table";
import { shareApi } from "../shareApi";
import type { ShareLinkRow } from "../shareApi";
/* 任务 83 §B2：时间格式化收敛到唯一出处（原先这个文件里自己写了一份 formatTime）。 */
import { formatLocalTime } from "../lib/datetime";

const STATUS_TAG: Record<string, { color: string; label: string }> = {
  active: { color: "green", label: "有效" },
  revoked: { color: "red", label: "已撤销" },
  expired: { color: "orange", label: "已过期" },
};

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export default function SharePanel({ courseId }: { courseId: string }) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["shares", courseId],
    queryFn: () => shareApi.list(courseId),
    retry: false,
  });
  const [label, setLabel] = useState("");
  const [days, setDays] = useState<number | null>(30);
  const [password, setPassword] = useState("");
  // 明文 token / url 只存在于创建它的这次会话里（后端只存哈希，之后取不回来）
  const [fresh, setFresh] = useState<Record<string, ShareLinkRow>>({});

  const create = useMutation({
    mutationFn: () => shareApi.create(courseId, {
      label,
      expires_in_days: days ?? null,
      password,
      base_url: window.location.origin,
    }),
    onSuccess: (row) => {
      setFresh((prev) => ({ ...prev, [row.id]: row }));
      setLabel("");
      setPassword("");
      message.success("已创建：明文链接只显示这一次，请立即复制");
      qc.invalidateQueries({ queryKey: ["shares", courseId] });
    },
    onError: (e) => message.error(`创建失败：${String(e)}`),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => shareApi.revoke(id),
    onSuccess: () => {
      message.success("已撤销，链接立即失效");
      qc.invalidateQueries({ queryKey: ["shares", courseId] });
    },
    onError: (e) => message.error(`撤销失败：${String(e)}`),
  });

  const remove = useMutation({
    mutationFn: (id: string) => shareApi.remove(id),
    onSuccess: () => {
      message.success("已删除分享记录");
      qc.invalidateQueries({ queryKey: ["shares", courseId] });
    },
    onError: (e) => message.error(`删除失败：${String(e)}`),
  });

  const doCopy = async (row: ShareLinkRow, url?: string) => {
    const text = url ?? fresh[row.id]?.url ?? "";
    if (!text) {
      message.warning("明文 token 只显示一次，已无法取回：请撤销后重新创建链接");
      return;
    }
    const ok = await copyText(text);
    message[ok ? "success" : "warning"](ok ? "链接已复制" : `请手动复制：${text}`);
  };

  const columns: ColumnsType<ShareLinkRow> = [
    {
      title: "备注",
      dataIndex: "label",
      render: (v: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{v || "（未填）"}</Typography.Text>
          {row.has_password ? <Tag color="purple">有密码</Tag> : null}
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (v: string) => {
        const meta = STATUS_TAG[v] ?? { color: "default", label: v };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
      { title: "有效期至", dataIndex: "expires_at", width: 150, render: (v: string | null) => (v ? formatLocalTime(v) : "不过期") },
    { title: "访问次数", dataIndex: "access_count", width: 90 },
      { title: "最近访问", dataIndex: "last_accessed_at", width: 150, render: (v: string | null) => formatLocalTime(v) },
    {
      title: "操作",
      key: "ops",
      width: 260,
      render: (_: unknown, row) => (
        <Space wrap>
          <Button
            size="small"
            icon={<CopyOutlined />}
            disabled={!fresh[row.id]?.url}
            onClick={() => doCopy(row)}
          >
            复制链接
          </Button>
          <Popconfirm
            title="撤销后客户立即看不到内容，确定？"
            okText="撤销"
            cancelText="取消"
            disabled={row.status === "revoked"}
            onConfirm={() => revoke.mutate(row.id)}
          >
            <Button size="small" danger type="text" icon={<StopOutlined />} disabled={row.status === "revoked"}>
              撤销
            </Button>
          </Popconfirm>
          <Popconfirm
            title="删除记录后不可恢复，确定？"
            okText="删除"
            cancelText="取消"
            onConfirm={() => remove.mutate(row.id)}
          >
            <Button size="small" danger type="text" icon={<DeleteOutlined />}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="只读分享链接：给客户看成果用"
        description="链接只能读这一门课、不能改任何内容；可设有效期与密码、可随时撤销，访问会留痕。明文 token 只在创建时显示一次，请立即复制保存。仅本机/内网可用，公网需内网穿透或 HTTPS 反代。"
      />
      <Card size="small" title={<Space><LinkOutlined />新建分享链接</Space>}>
        <Space wrap align="center">
          <Input
            placeholder="备注（给谁用）"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            style={{ width: 200 }}
          />
          <Space size={4}>
            <Typography.Text type="secondary">有效期</Typography.Text>
            <InputNumber
              min={1}
              max={3650}
              value={days ?? undefined}
              onChange={(v) => setDays(v ?? null)}
              placeholder="天"
              style={{ width: 90 }}
            />
            <Typography.Text type="secondary">天（留空 = 不过期）</Typography.Text>
          </Space>
          <Input.Password
            placeholder="访问密码（可留空）"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            style={{ width: 180 }}
          />
          <Button type="primary" loading={create.isPending} onClick={() => create.mutate()}>
            创建链接
          </Button>
        </Space>
        {Object.values(fresh).filter((row) => row.url).map((row) => (
          <Alert
            key={row.id}
            style={{ marginTop: 12 }}
            type="success"
            showIcon
            message="链接已生成（只显示这一次）"
            description={
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Typography.Text copyable={{ text: row.url }} code>{row.url}</Typography.Text>
                {row.token ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    token：{row.token}
                  </Typography.Text>
                ) : null}
                <Button size="small" icon={<CopyOutlined />} onClick={() => doCopy(row)}>复制链接</Button>
              </Space>
            }
          />
        ))}
      </Card>
      <Card size="small" title="已有链接">
        <Table<ShareLinkRow>
          rowKey="id"
          size="small"
          loading={isLoading}
          dataSource={data?.items ?? []}
          columns={columns}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有分享链接" /> }}
        />
      </Card>
    </Space>
  );
}
