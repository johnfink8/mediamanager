import { defineConfig } from "@playwright/test";

export default defineConfig({
    testDir: "./e2e",
    timeout: 30_000,
    retries: 1,
    use: {
        baseURL: process.env.BASE_URL || "http://localhost:4000",
        headless: true,
        screenshot: "only-on-failure",
        video: "retain-on-failure",
    },
    webServer: {
        command: "",
        url: process.env.BASE_URL || "http://localhost:4000",
        reuseExistingServer: true,
        timeout: 120_000,
    },
    reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
});
