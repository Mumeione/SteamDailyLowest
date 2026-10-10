/* jsdom 解析的**唯一一份**候选逻辑（smoke_s9.js / parity_filters.js 共用，勿在各脚本里
 * 复制第二份 —— 候选顺序一变就是两处同步）。jsdom 不在仓库依赖里：
 * 解析顺序 = SDL_NODE_MODULES 显式指路 → NODE_PATH → 全局 npm 目录 → 仓库根 node_modules。
 * 2026-10-10 起内置全局 npm 目录候选 —— 全局装一次，跑测试不再需要设 NODE_PATH。
 */
const path = require("path");
const os = require("os");

function loadJsdom() {
  const candidates = [];
  if (process.env.SDL_NODE_MODULES) candidates.push(process.env.SDL_NODE_MODULES);
  if (process.env.NODE_PATH) {
    process.env.NODE_PATH.split(path.delimiter).forEach((p) => { if (p) candidates.push(p); });
  }
  if (process.env.APPDATA) candidates.push(path.join(process.env.APPDATA, "npm", "node_modules"));
  candidates.push(path.join(os.homedir(), ".npm-global", "lib", "node_modules"));
  candidates.push("/usr/local/lib/node_modules");
  candidates.push("/usr/lib/node_modules");
  candidates.push(path.join(__dirname, "..", "..", "node_modules"));
  try {
    return require(require.resolve("jsdom", { paths: candidates }));
  } catch (err) {
    console.error("[jsdom] 找不到 jsdom（node 无法 require）。任选其一：");
    console.error("  ① 全局安装：npm install -g jsdom");
    console.error("  ② 仓库根：npm install --no-save jsdom");
    console.error("  ③ 指路：set SDL_NODE_MODULES=<含 jsdom 的 node_modules 目录>   (Windows)");
    console.error("     export SDL_NODE_MODULES=<...>/node_modules            (bash)");
    console.error("[jsdom] 尝试过的路径：" + candidates.join(" ; "));
    process.exit(2);
  }
}

module.exports = { loadJsdom };
