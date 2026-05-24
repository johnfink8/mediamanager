import { test, expect } from "@playwright/test";

test.describe("smoke: active items", () => {
    test("Movies page renders seeded movies", async ({ page }) => {
        await page.goto("/movies");
        await expect(
            page.getByText("Playwright Test Movie One").first()
        ).toBeVisible({ timeout: 15_000 });
        await expect(
            page.getByText("Playwright Test Movie Two").first()
        ).toBeVisible();
    });

    test("TV page renders seeded shows", async ({ page }) => {
        await page.goto("/tv");
        await expect(
            page.getByText("Playwright Test Show One").first()
        ).toBeVisible({ timeout: 15_000 });
        await expect(
            page.getByText("Playwright Test Show Two").first()
        ).toBeVisible();
    });

    test("sidebar nav switches between Movies and TV", async ({ page }) => {
        await page.goto("/movies");
        await page
            .getByText("Playwright Test Movie One")
            .first()
            .waitFor({ timeout: 15_000 });
        await page.locator(".sidebar").getByRole("button", { name: "TV" }).click();
        await expect(
            page.getByText("Playwright Test Show One").first()
        ).toBeVisible({ timeout: 15_000 });
    });

    test("active items show + Add / Defer / Ignore buttons", async ({
        page,
    }) => {
        await page.goto("/movies");
        await page
            .getByText("Playwright Test Movie One")
            .first()
            .waitFor({ timeout: 15_000 });
        await expect(
            page.getByRole("button", { name: "+ Add" }).first()
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Defer" }).first()
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Ignore" }).first()
        ).toBeVisible();
    });
});

test.describe("smoke: history section", () => {
    test("history page shows ignored movies by default", async ({ page }) => {
        await page.goto("/history");
        await expect(page.getByText("Dismissed Test Movie")).toBeVisible({
            timeout: 15_000,
        });
        await expect(page.getByText("Added Test Movie")).toBeVisible();
    });

    test("history page shows correct decision chips for movies", async ({
        page,
    }) => {
        await page.goto("/history");
        await page.getByText("Dismissed Test Movie").waitFor({ timeout: 15_000 });
        await expect(
            page.locator(".decision-tag.ignored").first()
        ).toBeVisible();
        await expect(page.locator(".decision-tag.added").first()).toBeVisible();
    });

    test("TV type filter shows ignored TV shows", async ({ page }) => {
        await page.goto("/history");
        await page
            .locator(".toolbar")
            .getByRole("button", { name: "TV" })
            .click();
        await expect(page.getByText("Dismissed Test Show")).toBeVisible({
            timeout: 15_000,
        });
        await expect(page.getByText("Added Test Show")).toBeVisible();
    });

    test("decision filter hides non-matching items", async ({ page }) => {
        await page.goto("/history");
        await page.getByText("Dismissed Test Movie").waitFor({ timeout: 15_000 });
        await page
            .locator(".toolbar")
            .getByRole("button", { name: "Added" })
            .click();
        await expect(page.getByText("Added Test Movie")).toBeVisible();
        await expect(page.getByText("Dismissed Test Movie")).not.toBeVisible();
    });

    test("toggle button flips decision status", async ({ page }) => {
        await page.goto("/history");
        await page.getByText("Dismissed Test Movie").waitFor({ timeout: 15_000 });
        const row = page.locator("tr", {
            has: page.getByText("Dismissed Test Movie"),
        });
        await row.getByRole("button", { name: "Mark added" }).click();
        await expect(row.locator(".decision-tag")).toHaveText("Added", {
            timeout: 5_000,
        });
        // restore
        await row.getByRole("button", { name: "Mark ignored" }).click();
        await expect(row.locator(".decision-tag")).toHaveText("Ignored", {
            timeout: 5_000,
        });
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

    test("historicalItems query returns decided items with createdAt", async ({
        request,
    }) => {
        const response = await request.post("/graphql", {
            data: {
                query: `{ historicalItems(itemType: "mv", limit: 10, offset: 0) { nodes { id title added createdAt } } }`,
            },
        });
        expect(response.ok()).toBeTruthy();
        const body = await response.json();
        expect(body.data.historicalItems.nodes.length).toBeGreaterThan(0);
    });
});
