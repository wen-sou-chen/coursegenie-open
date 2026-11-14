import type { ReactNode } from "react";
import { Card, Space } from "antd";
import type { CardProps } from "antd";

export interface SectionCardProps extends CardProps {
  /** 标题前的图标（统一卡片视觉） */
  icon?: ReactNode;
}

/**
 * 统一内容卡片：圆角 12px + 柔和阴影 + hover 上浮。
 * 与 antd Card 完全同构（props 透传），可无痛替换现有 <Card>。
 */
export default function SectionCard({ icon, title, className, ...rest }: SectionCardProps) {
  const mergedTitle =
    title === undefined ? undefined : icon ? (
      <Space size={8}>
        {icon}
        <span>{title}</span>
      </Space>
    ) : (
      title
    );
  return (
    <Card
      title={mergedTitle}
      className={`section-card${className ? ` ${className}` : ""}`}
      {...rest}
    />
  );
}
