import { createTheme } from "@mantine/core";

export const theme = createTheme({
  colors: {
    studio: ["#eef4ff", "#e0ebff", "#c5d9ff", "#9dbfff", "#73a3ff", "#4d86f7", "#246be8", "#1b55bd", "#19489b", "#183e7f"],
  },
  primaryColor: "studio",
  primaryShade: { light: 6, dark: 5 },
  defaultRadius: "md",
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
  headings: { fontWeight: "650" },
  cursorType: "pointer",
});
