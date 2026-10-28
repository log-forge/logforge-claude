import { LogForge, type Options } from "../../sdk/javascript/index.js";

interface Order { id: string; total: number; }
const order: Order = { id: "ts-order", total: 42 };
const options: Options = { tags: ["backend"], name: "checkout.typescript" };
LogForge.out("checkout", () => order, options);
LogForge.check("CHECKOUT", "checkout.total", () => order.total > 0, { tags: ["backend"] });
await LogForge.flush();
