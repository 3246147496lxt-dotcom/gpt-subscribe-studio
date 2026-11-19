import { build } from "esbuild";
import { fileURLToPath } from "node:url";

const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL("../tests/purchase-intent.test.ts", import.meta.url))],
  bundle: true,
  platform: "node",
  format: "esm",
  write: false,
});
await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);
