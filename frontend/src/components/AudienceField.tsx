import { AutoComplete } from "antd";

/** 常见受众只是输入框的候选，用户永远可以自己填别的。 */
export const AUDIENCE_OPTIONS = [
  "零基础成人",
  "职场新人",
  "在职从业者",
  "考证备考生",
  "大学生",
  "中学生",
  "小学生",
  "家长",
  "企业内训学员",
  "教师与教研员",
  "自由职业者",
].map((value) => ({ value }));

export const AUDIENCE_PLACEHOLDER = "例如：零基础成年人、在职会计、备考学员（可自己填）";
export const AUDIENCE_HINT = "影响讲义的口味与难度；可以随时改，不影响已生成内容";

type AudienceFieldProps = {
  value?: string;
  onChange?: (value: string) => void;
};

/** 目标受众输入：建课表单与大纲生成表单共用这一份，候选与说明只在这里改。 */
export default function AudienceField({ value, onChange }: AudienceFieldProps) {
  return (
    <div>
      <AutoComplete
        value={value}
        onChange={onChange}
        options={AUDIENCE_OPTIONS}
        placeholder={AUDIENCE_PLACEHOLDER}
        allowClear
        filterOption={(input, option) =>
          String(option?.value ?? "").toLowerCase().includes(input.toLowerCase())
        }
        style={{ width: "100%" }}
      />
      <div style={{ marginTop: 4, fontSize: 12, color: "#8c8c8c", lineHeight: 1.5 }}>
        {AUDIENCE_HINT}
      </div>
    </div>
  );
}
