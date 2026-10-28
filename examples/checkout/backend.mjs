import http from "node:http";
import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { LogForge } from "../../sdk/javascript/index.js";

const files = new Map([["/", "index.html"], ["/frontend.js", "frontend.js"], ["/sdk.js", "../../sdk/javascript/index.js"]]);
const server = http.createServer(async (request, response) => {
  try {
    if (request.url === "/health") { response.end("ready"); return; }
    if (request.url === "/config") {
      // This sample is a loopback-only development app; production never serves this route.
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ tags: (process.env.LOGFORGE_TAGS || "").split(",").filter(Boolean),
        level: process.env.LOGFORGE_LEVEL || "debug", runId: process.env.LOGFORGE_RUN_ID,
        collectorUrl: process.env.LOGFORGE_COLLECTOR_URL, token: process.env.LOGFORGE_COLLECTOR_TOKEN }));
      return;
    }
    if (request.method === "POST" && request.url === "/checkout") {
      LogForge.out("Checkout", () => ({ cart_id: "cart-demo", quantity: 2 }), { name: "checkout.received", tags: ["backend"] });
      LogForge.out("catalog", () => ({ inventory_refresh: true }), { name: "catalog.background", tags: ["backend"] });
      const python = process.env.LOGFORGE_PYTHON || (process.platform === "win32" ? "python" : "python3");
      const child = spawn(python, [fileURLToPath(new URL("database.py", import.meta.url))], { env: { ...process.env, LOGFORGE_SERVICE: "database" } });
      let output = "";
      child.stdout.on("data", data => { output += data; process.stdout.write(data); });
      child.stderr.on("data", data => process.stderr.write(data));
      const code = await new Promise((resolve, reject) => { child.once("exit", resolve); child.once("error", reject); });
      if (code !== 0) throw new Error("Database check process failed");
      const result = JSON.parse(output.split("\n").find(line => line.startsWith("RESULT ")).slice(7));
      LogForge.out("checkout", () => result, { name: "checkout.responded", tags: ["backend"] });
      await LogForge.flush();
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(result));
      return;
    }
    if (files.has(request.url)) {
      response.setHeader("Content-Type", request.url.endsWith(".js") ? "text/javascript" : "text/html");
      response.end(await readFile(new URL(files.get(request.url), import.meta.url)));
      return;
    }
    response.statusCode = 404; response.end();
  } catch (error) {
    LogForge.error("checkout", () => error, { name: "checkout.failed", tags: ["backend"] });
    response.statusCode = 500; response.end("Checkout failed");
  }
});
server.listen(Number(process.env.PORT || 38500), process.env.HOST || "127.0.0.1");
