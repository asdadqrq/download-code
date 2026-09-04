import fs from "node:fs/promises";
import { Workbook } from "@oai/artifact-tool";

const csvPath = "/Users/fangdingding/PycharmProjects/PythonProject/115_exists_check.csv";
const previewPath = "/private/tmp/115_exists_check_preview.png";
const csvText = await fs.readFile(csvPath, "utf8");
const workbook = await Workbook.fromCSV(csvText, { sheetName: "115 Check" });
const inspection = await workbook.inspect({
  kind: "table",
  range: "115 Check!A1:H17",
  include: "values,formulas",
  tableMaxRows: 20,
  tableMaxCols: 8,
});
const preview = await workbook.render({
  sheetName: "115 Check",
  range: "A1:H17",
  scale: 1,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
console.log(inspection.ndjson);
console.log(previewPath);
