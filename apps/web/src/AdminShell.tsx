import "@mantine/core/styles.css";
import "./styles.css";

import { MantineProvider } from "@mantine/core";

import { App } from "./App";
import { theme } from "./theme";

export default function AdminShell() {
  return (
    <MantineProvider theme={theme} defaultColorScheme="light">
      <App />
    </MantineProvider>
  );
}
