import { test, expect } from "@playwright/test";

test.describe("smoke: active items", () => {
    test("Movies tab renders seeded movies", async ({ page }) => {
        await page.goto("/");
        await expect(page.getByText("Playwright Test Movie One").first()).toBeVisible({ timeout: 15_000 });
        await expect(page.getByText("Playwright Test Movie Two").first()).toBeVisible();
    });

    test("TV tab renders seeded shows", async ({ page }) => {
        await page.goto("/");
        await page.getByText("Playwright Test Movie One").first().waitFor({ timeout: 15_000 });
        await page.getByRole("button", { name: "TV" }).click();
        await expect(page.getByText("Playwright Test Show One").first()).toBeVisible({ timeout: 15_000 });
        await expect(page.getByText("Playwright Test Show Two").first()).toBeVisible();
    });

    test("active items show Add/Ignore/Defer buttons", async ({ page }) => {
        await page.goto("/");
        await page.getByText("Playwright Test Movie One").waitFor({ timeout: 15_000 });
        await expect(page.getByRole("button", { name: "Add" }).first()).toBeVisible();
        await expect(page.getByRole("button", { name: "Ignore" }).first()).toBeVisible();
        await expect(page.getByRole("button", { name: "Defer" }).first()).toBeVisible();
    });
});

test.describe("smoke: history section", () => {
    test("Movie History shows ignored movies", async ({ page }) => {
        await page.goto("/");
        await page.getByRole("button", { name: "Item History" }).click();
        await page.getByRole("button", { name: "Movie History" }).click();
        await expect(page.getByText("Dismissed Test Movie")).toBeVisible({ timeout: 15_000 });
        await expect(page.getByText("Added Test Movie")).toBeVisible();
    });

    test("Movie History shows correct status chips", async ({ page }) => {
        await page.goto("/");
        await page.getByRole("button", { name: "Item History" }).click();
        await page.getByRole("button", { name: "Movie History" }).click();
        await page.getByText("Dismissed Test Movie").waitFor({ timeout: 15_000 });
        await expect(page.getByText("Dismissed").first()).toBeVisible();
        await expect(page.getByText("Marked Added").first()).toBeVisible();
    });

    test("Show History shows ignored TV shows", async ({ page }) => {
        await page.goto("/");
        await page.getByRole("button", { name: "Item History" }).click();
        await page.getByRole("button", { name: "Show History" }).click();
        await expect(page.getByText("Dismissed Test Show")).toBeVisible({ timeout: 15_000 });
        await expect(page.getByText("Added Test Show")).toBeVisible();
    });
});

test.describe("smoke: GraphQL API", () => {
    test("items query returns active items", async ({ request }) => {
        const response = await request.post("/graphql", {
            data: { query: `{ items { nodes { id type title } } }` },
        });
        expect(response.ok()).toBeTruthy();
        const body = await response.json();
        expect(body.data.items.nodes.length).toBeGreaterThan(0);
    });
});
