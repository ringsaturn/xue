import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export default function prepareBundleFixture(): void {
  const repositoryRoot = fileURLToPath(new URL("../..", import.meta.url));
  const python = process.env.PYTHON || ".venv/bin/python";
  const result = spawnSync(python, ["tests/prepare_web_fixture.py"], {
    cwd: repositoryRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    const details = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
    throw new Error(
      `failed to prepare Xue fixture with ${python}: ${details || `exit status ${result.status}`}`,
    );
  }
}
