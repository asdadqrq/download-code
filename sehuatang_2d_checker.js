/*
 * sehuatang.net batch 2D checker
 *
 * Run this in Chrome DevTools Console while you are logged in and viewing any
 * sehuatang.net page. It uses the browser's existing login session.
 */
(async () => {
  "use strict";

  if (location.hostname !== "sehuatang.net") {
    alert("Open sehuatang.net first, then run this script there.");
    return;
  }

  const input = prompt(
    "Enter codes separated by commas, spaces, or new lines:",
    "vrkm-540,vrkm-541"
  );
  if (!input) return;

  const codes = [...new Set(
    input
      .split(/[\s,，;；]+/)
      .map((value) => value.trim().toUpperCase())
      .filter(Boolean)
  )];

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const escapeRegExp = (value) =>
    value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // Prevent VRKM-540 from matching a fuzzy result such as VRKM-1540.
  const containsExactCode = (title, code) => {
    const pattern = new RegExp(
      `(^|[^A-Z0-9])${escapeRegExp(code)}($|[^A-Z0-9])`,
      "i"
    );
    return pattern.test(title);
  };

  const results = [];

  for (let index = 0; index < codes.length; index += 1) {
    const code = codes[index];
    console.log(`[${index + 1}/${codes.length}] Searching ${code}...`);

    try {
      const url = new URL("/search.php", location.origin);
      url.search = new URLSearchParams({
        mod: "forum",
        srchtxt: code,
        searchsubmit: "yes",
        orderby: "lastpost",
        ascdesc: "desc"
      });

      const response = await fetch(url, {
        credentials: "include",
        redirect: "follow"
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const titleLinks = [
        ...doc.querySelectorAll(
          '#threadlist h3 a, .pbw h3 a, h3 a[href*="viewthread"]'
        )
      ];
      const titles = [...new Set(
        titleLinks.map((link) => link.textContent.trim()).filter(Boolean)
      )];
      const exactTitles = titles.filter((title) => containsExactCode(title, code));
      const matchingTitles = exactTitles.filter((title) => /2d/i.test(title));

      results.push({
        code,
        contains2D: matchingTitles.length > 0 ? "YES" : "NO",
        exactResults: exactTitles.length,
        matchingTitles: matchingTitles.join(" | ") || "-"
      });
    } catch (error) {
      results.push({
        code,
        contains2D: "ERROR",
        exactResults: 0,
        matchingTitles: error instanceof Error ? error.message : String(error)
      });
    }

    // This forum enforces a 12-second gap between searches.
    if (index < codes.length - 1) await sleep(11000);
  }

  console.table(results);

  const summary = results
    .map(
      ({ code, contains2D, exactResults, matchingTitles }) =>
        `${code}: ${contains2D} (exact results: ${exactResults})` +
        (contains2D === "YES" ? `\n  ${matchingTitles}` : "")
    )
    .join("\n");

  console.log("\n" + summary);
  try {
    await navigator.clipboard.writeText(summary);
    console.log("Summary copied to clipboard.");
  } catch {
    console.log("Clipboard copy was blocked; copy the summary above manually.");
  }
})();
