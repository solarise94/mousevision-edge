/* 本地版数据导出（mobile.js 内 buildExportCsv / buildExportJson / utf8ToBase64 /
 * countRecords / exportFilename）单元测试。
 * mobile.js 是浏览器 IIFE，不导出模块；这里用 brace 匹配精确提取命名纯函数，
 * 在 node 里独立 eval，验证：
 *   - CSV 列顺序 / UTF-8 BOM / RFC4180 转义（含逗号、引号、换行）
 *   - JSON 导出带 format 版本字段、照片 dataURL 原样保留
 *   - base64 UTF-8 编码往返（中文/emoji 均不丢字节）
 *   - 空数据分支（countRecords 为 0；buildExportCsv 仅剩表头）
 *   - 文件名 小鼠称重_YYYYMMDD_HHmm.<ext>
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(__dirname, "../../ui/static/mobile.js"), "utf8");

/* ---------- 从 mobile.js 提取命名函数（brace 匹配） ---------- */
function extractFunction(src, name) {
  const re = new RegExp("function\\s+" + name + "\\s*\\([^)]*\\)\\s*\\{");
  const m = src.match(re);
  if (!m) throw new Error("function not found: " + name);
  let depth = 0;
  for (let j = m.index + m[0].length - 1; j < src.length; j++) {
    if (src[j] === "{") depth += 1;
    else if (src[j] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(m.index, j + 1);
    }
  }
  throw new Error("unbalanced braces: " + name);
}

/* 提取的纯函数：csvEscape / buildExportCsv / buildExportJson / countRecords /
 * exportFilename / utf8ToBase64。utf8ToBase64 内部引用 utf8Encode 与
 * base64FromBytes，一并提取。 */
const names = ["csvEscape", "utf8Encode", "base64FromBytes", "utf8ToBase64",
  "buildExportCsv", "buildExportJson", "countRecords", "exportFilename"];
const helpers = {};
{
  // 与 mobile.js 常量保持一致（buildExportCsv / buildExportJson 引用）
  const CSV_HEADERS = ["cage_id", "project_id", "strain", "ordinal", "weight_g",
    "recorded_at", "weight_source", "record_id", "run_id", "created_at"];
  const EXPORT_FORMAT = "miceautomatic-export-v1";
  const code = names.map((n) => extractFunction(SRC, n)).join("\n") +
    "\n" + names.map((n) => `helpers.${n} = ${n};`).join("\n");
  eval(code); // eslint-disable-line no-eval
}
const { csvEscape, utf8ToBase64, buildExportCsv, buildExportJson, countRecords, exportFilename } = helpers;

/* 一个样例数据集：两箱、含特殊字符字段、记录带 run_id。 */
function sampleData() {
  return {
    boxes: [
      { cage_id: "C57-A1", project_id: "P1", strain: "C57BL/6" },
      { cage_id: "BALB-B2", project_id: "P2", strain: "BALB/c" },
    ],
    recordsByCage: {
      "C57-A1": [
        {
          record_id: "r1", ordinal: 1, weight_g: 20.5, recorded_at: "2026-08-07T10:00:00",
          weight_source: "bluetooth", run_id: "local_1_ab", created_at: "2026-08-07T10:00:01",
        },
        // 含逗号 / 引号 / 换行的字段，验证 CSV 转义
        {
          record_id: "r2", ordinal: 2, weight_g: 21, recorded_at: "2026-08-07T10:05:00",
          weight_source: "手动,半自动", run_id: "local_2_cd", created_at: "2026-08-07T10:05:01",
        },
      ],
      "BALB-B2": [
        {
          record_id: "r3", ordinal: 1, weight_g: 19.2, recorded_at: "2026-08-07T11:00:00",
          weight_source: "manual", run_id: "local_3_ef", created_at: "2026-08-07T11:00:01",
        },
      ],
    },
    exportedAt: "2026-08-07T12:00:00",
  };
}

test("CSV: BOM 前缀 + 表头列顺序", () => {
  const csv = buildExportCsv(sampleData());
  assert.ok(csv.startsWith("\ufeff"));
  const firstLine = csv.slice(1).split("\n")[0];
  assert.equal(firstLine, "cage_id,project_id,strain,ordinal,weight_g,recorded_at,weight_source,record_id,run_id,created_at");
});

test("CSV: 每行 10 列，box 元数据并入行", () => {
  const csv = buildExportCsv(sampleData());
  const lines = csv.replace(/^\ufeff/, "").split("\n").filter((l) => l.length > 0);
  assert.equal(lines.length, 1 + 3); // 表头 + 3 条记录
  // 第 2 行是 C57-A1 的 r1：cage 与 box 元数据应并入
  assert.ok(lines[1].includes("C57-A1"));
  assert.ok(lines[1].includes("P1"));
  assert.ok(lines[1].includes("C57BL/6"));
});

test("CSV: RFC4180 转义（逗号/引号/换行）", () => {
  assert.equal(csvEscape("plain"), "plain");
  assert.equal(csvEscape('has,comma'), '"has,comma"');
  assert.equal(csvEscape('has"quote'), '"has""quote"');
  assert.equal(csvEscape("line\nbreak"), '"line\nbreak"');
  assert.equal(csvEscape("a\r\nb"), '"a\r\nb"');
  assert.equal(csvEscape(20.5), "20.5");
  assert.equal(csvEscape(null), "");
  assert.equal(csvEscape(undefined), "");

  // 集成：weight_source="手动,半自动" 应整列加引号
  const csv = buildExportCsv(sampleData());
  assert.ok(csv.includes('"手动,半自动"'));
});

test("CSV: 转义字段在解析后字节正确（含中文 UTF-8）", () => {
  // 用 UTF-8 读取行，确认中文与引号原样保留
  const csv = buildExportCsv(sampleData());
  const body = csv.replace(/^\ufeff/, "").split("\n");
  const line = body.find((l) => l.includes("手动"));
  assert.ok(line);
  assert.ok(line.includes('"手动,半自动"'));
  assert.ok(new TextEncoder().encode(csv).length > csv.length); // 确有非 ASCII 字节
});

test("JSON: 含 format 版本字段 + 照片 dataURL 原样保留", () => {
  const data = sampleData();
  // 注入一条带照片 dataURL 的记录
  data.recordsByCage["C57-A1"][0].photo = "data:image/jpeg;base64,AAAA";
  const json = buildExportJson(data);
  const parsed = JSON.parse(json);
  assert.equal(parsed.format, "miceautomatic-export-v1");
  assert.equal(parsed.exportedAt, "2026-08-07T12:00:00");
  assert.equal(parsed.recordsByCage["C57-A1"][0].photo, "data:image/jpeg;base64,AAAA");
  assert.deepEqual(parsed.boxes, data.boxes);
});

test("base64 UTF-8: 中文/emoji 编码往返字节一致", () => {
  const utf8 = (s) => new TextEncoder().encode(s);
  const fromB64 = (b64) => Buffer.from(b64, "base64");

  const samples = ["小鼠称重_20260807_1000.csv", "hello", "中文,含逗号", "🐭🧀", '引号"和换行\n'];
  for (const s of samples) {
    const b64 = utf8ToBase64(s);
    const round = fromB64(b64);
    assert.deepEqual(new Uint8Array(round), new Uint8Array(utf8(s)), `roundtrip fail: ${s}`);
  }
});

test("base64 UTF-8: 与标准 btoa 结果一致", () => {
  // node 有 btoa（v23），但 mobile.js 的 utf8ToBase64 会走到 btoa 分支；
  // 这里构造 ASCII 样例验证 btoa 分支一致
  const s = "ASCII only 123";
  const expected = Buffer.from(s, "utf8").toString("base64");
  assert.equal(utf8ToBase64(s), expected);
});

test("空数据：countRecords 为 0；buildExportCsv 仅剩表头", () => {
  assert.equal(countRecords({ boxes: [], recordsByCage: {} }), 0);
  assert.equal(countRecords(sampleData()), 3);
  assert.equal(countRecords({}), 0);
  const csv = buildExportCsv({ boxes: [], recordsByCage: {} });
  assert.ok(csv.startsWith("\ufeff"));
  assert.equal(csv.replace(/^\ufeff/, "").trim(), "cage_id,project_id,strain,ordinal,weight_g,recorded_at,weight_source,record_id,run_id,created_at");
});

test("文件名：小鼠称重_YYYYMMDD_HHmm.<ext>", () => {
  // 用固定时间注入不可行（函数内部用 new Date()），只做形状校验
  const m = exportFilename("csv").match(/^小鼠称重_\d{8}_\d{4}\.csv$/);
  assert.ok(m, exportFilename("csv"));
  const mj = exportFilename("json").match(/^小鼠称重_\d{8}_\d{4}\.json$/);
  assert.ok(mj, exportFilename("json"));
});
