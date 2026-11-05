/**
 * Load a frontend TypeScript module in plain Node (no test framework, no new
 * dependency): transpile it with the project's own `typescript` devDependency
 * and import the result. Works on Node 20 (CI) and later.
 *
 * Only for modules with no runtime imports (type-only imports are erased),
 * which is what lib/research.ts is.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const ts = createRequire(join(FRONTEND, "package.json"))("typescript");

export async function loadTs(relPath) {
  const source = readFileSync(join(FRONTEND, relPath), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    fileName: relPath,
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
}
