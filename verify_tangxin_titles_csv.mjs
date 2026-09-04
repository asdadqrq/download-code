import fs from "node:fs/promises";
import { Workbook } from "@oai/artifact-tool";

const csvPath = "/Users/fangdingding/PycharmProjects/PythonProject/tangxin_小欣奈_titles.csv";
const previewPath = "/private/tmp/tangxin_titles_preview.png";
const csvText = await fs.readFile(csvPath, "utf8");
const workbook = await Workbook.fromCSV(csvText, { sheetName: "Titles" });

const inspection = await workbook.inspect({
  kind: "table",
  range: "Titles!A1:A53",
  include: "values,formulas",
  tableMaxRows: 10,
  tableMaxCols: 1,
  maxChars: 5000,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
const preview = await workbook.render({
  sheetName: "Titles",
  range: "A1:A53",
  scale: 1,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
console.log(inspection.ndjson);
console.log(errors.ndjson);
console.log(previewPath);
