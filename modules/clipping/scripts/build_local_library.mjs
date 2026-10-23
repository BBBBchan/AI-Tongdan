#!/usr/bin/env node
// Build the local client with its own locked dependencies. No account data is read.
import { mkdir, copyFile, readdir, readFile, rename, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("..", import.meta.url));
const clientRoot = path.join(root, "local-library");
const require = createRequire(path.join(clientRoot, "package.json"));
const { build } = await import(pathToFileURL(require.resolve("vite")).href);
const { default: react } = await import(pathToFileURL(require.resolve("@vitejs/plugin-react")).href);
const { default: tailwind } = await import(pathToFileURL(require.resolve("@tailwindcss/postcss")).href);
const staging = path.join(root, "data/local-library/build");
const target = path.join(root, "static/clips");
await build({
  configFile: false,
  root: clientRoot,
  base: "./",
  publicDir: false,
  plugins: [react()],
  css: { postcss: { plugins: [tailwind()] } },
  build: { outDir: staging, emptyOutDir: true },
});
await mkdir(path.join(target, "assets"), { recursive: true });
for (const file of await readdir(path.join(staging, "assets"))) {
  await copyFile(path.join(staging, "assets", file), path.join(target, "assets", file));
}
for (const file of ["xiaodan.png", "favicon.svg"]) {
  await copyFile(path.join(clientRoot, "public", file), path.join(target, file));
}
const notices = ["Third-party licenses for the bundled local clip library.\n"];
for (const name of ["react", "react-dom", "scheduler", "lucide-react", "tailwindcss"]) {
  const directory = path.dirname(require.resolve(`${name}/package.json`));
  const info = JSON.parse(await readFile(path.join(directory, "package.json"), "utf8"));
  notices.push(`${name} ${info.version}\n${"=".repeat(60)}\n${await readFile(path.join(directory, "LICENSE"), "utf8")}`);
}
await writeFile(path.join(target, "THIRD_PARTY_NOTICES.txt"), notices.join("\n\n"));
// Switch index last, preserving the local manifest and assets for open tabs.
// library.json is runtime data: this build never reads or writes it.
await copyFile(path.join(staging, "index.html"), path.join(target, "index.html.tmp"));
await rename(path.join(target, "index.html.tmp"), path.join(target, "index.html"));
console.log("Local clip library built: static/clips/index.html");
