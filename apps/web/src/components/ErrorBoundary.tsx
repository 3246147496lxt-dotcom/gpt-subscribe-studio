import { Alert, Button, Card, Stack, Text } from "@mantine/core";
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  title?: string;
  children: ReactNode;
}

interface State {
  hasError: boolean;
  message: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: "" };

  static getDerivedStateFromError(error: Error): State {
    return {
      hasError: true,
      message: error?.message || "页面组件渲染异常",
    };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo) {
    // Intentionally omit raw error/props logging to avoid leaking transient session/card inputs.
  }

  private handleReset = () => {
    this.setState({ hasError: false, message: "" });
  };

  render() {
    if (this.state.hasError) {
      return (
        <Card withBorder radius="lg" p="lg" className="workspace-panel">
          <Stack gap="sm">
            <Alert color="red" title={this.props.title || "该功能面板加载失败"}>
              <Text size="sm">{this.state.message}</Text>
            </Alert>
            <div>
              <Button size="xs" variant="default" onClick={this.handleReset}>
                重试加载面板
              </Button>
            </div>
          </Stack>
        </Card>
      );
    }
    return this.props.children;
  }
}
