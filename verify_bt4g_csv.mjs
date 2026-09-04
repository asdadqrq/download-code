import fs from "node:fs/promises";
import { Workbook } from "@oai/artifact-tool";

const csvPath = "/Users/fangdingding/PycharmProjects/PythonProject/bt4g_exists_check.csv";
const previewPath = "/private/tmp/bt4g_exists_check_preview.png";
const csvText = await fs.readFile(csvPath, "utf8");
const workbook = await Workbook.fromCSV(csvText, { sheetName: "BT4G Check" });
const inspection = await workbook.inspect({
  kind: "table",
  range: "BT4G Check!A1:F3",
  include: "values,formulas",
  tableMaxRows: 5,
  tableMaxCols: 6,
});
const preview = await workbook.render({
  sheetName: "BT4G Check",
  range: "A1:F3",
  scale: 1,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
console.log(inspection.ndjson);
console.log(previewPath);
