import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const root = process.cwd();

function markdownFiles(relativeRoot) {
  const absoluteRoot = path.join(root, relativeRoot);
  if (!fs.existsSync(absoluteRoot)) return [];
  const result = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(absolute);
      else if (entry.name.endsWith(".md")) result.push(path.relative(root, absolute));
    }
  };
  visit(absoluteRoot);
  return result;
}

const publicDocs = [
  "README.md",
  "README.en.md",
  "SETUP.md",
  "SETUP.en.md",
  ...markdownFiles("docs"),
  "apps/api/README.md",
  "apps/api/README.zh-CN.md",
  "packages/installer/README.md",
  "packages/installer/README.zh-CN.md",
];

const failures = [];
const linkPattern = /!?\[[^\]]*\]\(([^)]+)\)/g;
const mixedSectionMarkers = [
  "## 中文摘要",
  "## 中文正文",
  "## English Summary",
  "## English Overview",
];

for (const relativeFile of publicDocs) {
  const absoluteFile = path.join(root, relativeFile);
  if (!fs.existsSync(absoluteFile)) {
    failures.push(`${relativeFile}: missing public document`);
    continue;
  }
  const content = fs.readFileSync(absoluteFile, "utf8");

  for (const marker of mixedSectionMarkers) {
    if (content.includes(marker)) {
      failures.push(`${relativeFile}: mixed-language section marker ${JSON.stringify(marker)}`);
    }
  }

  let inFence = false;
  for (const line of content.split("\n")) {
    if (line.trimStart().startsWith("```")) {
      inFence = !inFence;
      continue;
    }
    if (!inFence && /^#{1,6} .* \/ .*/.test(line)) {
      failures.push(`${relativeFile}: bilingual heading ${JSON.stringify(line)}`);
    }
  }

  for (const match of content.matchAll(linkPattern)) {
    let target = match[1].trim().replace(/^<|>$/g, "");
    target = target.split(/\s+["']/)[0];
    if (!target || target.startsWith("#") || /^(https?:|mailto:)/.test(target)) continue;
    const localPath = decodeURIComponent(target.split("#")[0]);
    const resolved = path.resolve(path.dirname(absoluteFile), localPath);
    if (!fs.existsSync(resolved)) {
      failures.push(`${relativeFile}: broken local link ${JSON.stringify(match[1])}`);
    }
  }
}

const requiredPairs = [
  ["README.md", "README.en.md"],
  ["SETUP.md", "SETUP.en.md"],
  ["docs/README.md", "docs/README.en.md"],
  ["docs/zh-CN/getting-started.md", "docs/en/getting-started.md"],
  ["docs/zh-CN/user-guide.md", "docs/en/user-guide.md"],
  ["docs/zh-CN/architecture.md", "docs/en/architecture.md"],
  ["docs/zh-CN/workflows.md", "docs/en/workflows.md"],
  ["docs/zh-CN/model-providers.md", "docs/en/model-providers.md"],
  ["docs/zh-CN/development.md", "docs/en/development.md"],
  ["apps/api/README.zh-CN.md", "apps/api/README.md"],
  ["packages/installer/README.zh-CN.md", "packages/installer/README.md"],
];

for (const pair of requiredPairs) {
  for (const relativeFile of pair) {
    if (!fs.existsSync(path.join(root, relativeFile))) {
      failures.push(`language pair is incomplete: ${pair.join(" <-> ")}`);
      break;
    }
  }
}

if (failures.length > 0) {
  console.error(failures.join("\n"));
  process.exit(1);
}

console.log(`Documentation checks passed (${publicDocs.length} public Markdown files).`);
