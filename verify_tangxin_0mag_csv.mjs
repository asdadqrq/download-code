import fs from "node:fs/promises";
import { Workbook } from "@oai/artifact-tool";

const csvPath = "/Users/fangdingding/PycharmProjects/PythonProject/tangxin_xiaoxinnai_0mag.csv";
const previewPath = "/private/tmp/tangxin_xiaoxinnai_0mag_preview.png";
const csvText = await fs.readFile(csvPath, "utf8");
const workbook = await Workbook.fromCSV(csvText, { sheetName: "0mag Search" });

const inspection = await workbook.inspect({
  kind: "table",
  range: "0mag Search!A1:H53",
  include: "values,formulas",
  tableMaxRows: 8,
  tableMaxCols: 8,
  maxChars: 7000,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
const preview = await workbook.render({
  sheetName: "0mag Search",
  range: "A1:H53",
  scale: 1,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
console.log(inspection.ndjson);
console.log(errors.ndjson);
console.log(previewPath);
