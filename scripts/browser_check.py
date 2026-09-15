"""用真实浏览器跑通离线筛选、复核与审计时间线。"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--sample", required=True, type=Path)
    parser.add_argument("--screenshot", required=True, type=Path)
    arguments = parser.parse_args()
    arguments.screenshot.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
        page.on(
            "console",
            lambda message: errors.append(f"console: {message.text}")
            if message.type == "error"
            else None,
        )

        page.goto(f"{arguments.base_url}/frontend/index.html", wait_until="networkidle")
        page.get_by_text("作品集演示", exact=True).wait_for()
        page.set_input_files("#fileInput", str(arguments.sample.resolve()))
        page.locator("#roleSelect").wait_for(state="visible")
        page.select_option("#roleSelect", value="AI 应用工程师")
        page.locator("#jobInfoCard:not(.hidden)").wait_for()
        page.click("#createTaskBtn")

        page.wait_for_url("**/criteria.html", timeout=20_000)
        page.wait_for_load_state("networkidle")
        page.locator("#criteriaBody tr").first.wait_for()
        page.click("#confirmBtn")

        page.wait_for_url("**/results.html", timeout=30_000)
        page.wait_for_load_state("networkidle")
        page.locator('tr[data-resume-id="R001"] button').click()
        page.wait_for_url("**/detail.html**", timeout=15_000)
        page.wait_for_load_state("networkidle")
        page.locator("#candidateContent:not(.hidden)").wait_for()
        page.click("#submitReviewBtn")

        page.wait_for_url("**/results.html", timeout=15_000)
        page.wait_for_load_state("networkidle")
        page.locator('tr[data-resume-id="R001"] button').click()
        page.wait_for_url("**/detail.html**", timeout=15_000)
        page.wait_for_load_state("networkidle")
        page.get_by_text("HR 已完成复核", exact=True).wait_for(timeout=15_000)
        page.screenshot(path=str(arguments.screenshot), full_page=True)
        assert page.locator("#auditTimeline .audit-event").count() >= 3
        assert "原始记录不覆盖" in page.locator("#auditCard").inner_text()
        browser.close()

    if errors:
        raise AssertionError("浏览器控制台出现错误：\n" + "\n".join(errors))
    print(f"AI browser flow OK: {arguments.screenshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
