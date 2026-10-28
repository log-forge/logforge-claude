import { LogForge } from "/sdk.js";

const config = await fetch("/config").then(response => response.json());
LogForge.configure({ tags: config.tags, level: config.level, runId: config.runId, service: "frontend",
  sink: LogForge.httpSink(config.collectorUrl, config.token) });
const button = document.querySelector("button");
const status = document.querySelector("#status");
button.disabled = false;
button.addEventListener("click", async () => {
  LogForge.out("checkout", () => ({ button: "Place order" }), { name: "checkout.clicked", tags: ["frontend"] });
  const response = await fetch("/checkout", { method: "POST" });
  const order = await response.json();
  LogForge.check("checkout", "checkout.ui-success", () => response.ok && order.status === "saved", { tags: ["frontend"] });
  status.textContent = response.ok ? `Order ${order.order_id} saved` : "Checkout failed";
  await LogForge.flush();
  status.dataset.ready = "true";
});
