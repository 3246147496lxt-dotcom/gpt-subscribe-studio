import { build } from "esbuild";
import Module from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const filename = resolve(root, ".frontend-utils-tests.cjs");
const result = await build({
  absWorkingDir: root,
  entryPoints: ["tests/frontend-utils.test.ts"],
  outfile: filename,
  bundle: true,
  packages: "external",
  platform: "node",
  format: "cjs",
  write: false,
});
const compiled = new Module(filename);
compiled.filename = filename;
compiled.paths = Module._nodeModulePaths(root);
compiled._compile(result.outputFiles[0].text, filename);
