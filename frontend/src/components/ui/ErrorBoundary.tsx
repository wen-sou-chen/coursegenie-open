/**
 * 任务 48x：单个面板的兜底边界。
 *
 * 起因：「高级工具 → 用量与成本」里 `data.aux_history.map` 抛 TypeError，
 * React 卸载整棵树 ⇒ 整页白屏，用户连「是哪个面板坏了」都看不到。
 * 这里给每个面板各包一层：出错只把该面板显示成一句提示 + 原始错误文本，
 * 页面框架与其它面板不受影响。
 */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { Alert } from "antd";

type Props = { name?: string; children: ReactNode };
type State = { error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[面板出错] ${this.props.name ?? "(未命名)"}`, error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (error) {
      return (
        <Alert
          type="error"
          showIcon
          message={`这个面板出错了${this.props.name ? `（${this.props.name}）` : ""}：${error.message}`}
          description="其它面板不受影响，可以切到别的页签继续用；把上面这段错误文本发给开发者即可定位。"
        />
      );
    }
    return this.props.children;
  }
}
