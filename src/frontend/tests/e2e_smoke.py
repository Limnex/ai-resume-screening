import argparse
import csv
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


def write_one_resume_csv(path: Path) -> None:
    headers = [
        "resume_id",
        "resume_text",
        "resume_skills",
        "experience_years",
        "projects",
        "certifications",
        "job_role",
        "required_skills",
        "job_experience_required",
        "job_description",
    ]
    row = {
        "resume_id": "E2E-001",
        "resume_text": "5年后端开发经验，熟悉 Python、FastAPI 和 REST API，负责过服务稳定性建设。",
        "resume_skills": "Python, FastAPI, REST API",
        "experience_years": "5",
        "projects": "负责招聘平台后端服务开发与上线",
        "certifications": "",
        "job_role": "后端工程师",
        "required_skills": "Python, FastAPI, REST API",
        "job_experience_required": "3",
        "job_description": "招聘后端工程师，要求3年以上经验，熟悉 Python、FastAPI 和 REST API。",
    }
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerow(row)


def wait_for_network(page, url: str, timeout: int = 30_000) -> None:
    page.goto(url, wait_until="networkidle", timeout=timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description="前后端真实流程浏览器冒烟测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--screenshot", default=".pytest-tmp/frontend-e2e.png")
    parser.add_argument("--existing-job-id", help="复用已完成的单简历任务，跳过真实模型调用")
    parser.add_argument("--existing-criteria-job-id", help="复用已提取条件的任务，从确认条件继续")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    screenshot = Path(args.screenshot).resolve()
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="resume-e2e-") as temp_dir:
        csv_path = Path(temp_dir) / "one-resume.csv"
        write_one_resume_csv(csv_path)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )

            frontend_url = f"{args.base_url.rstrip('/')}/frontend"
            wait_for_network(page, f"{frontend_url}/index.html")
            assert page.locator("#roleSelect option", has_text="全部岗位").count() == 0
            if args.existing_job_id:
                page.evaluate(
                    "jobId => localStorage.setItem('currentJobId', jobId)",
                    args.existing_job_id,
                )
                wait_for_network(page, f"{frontend_url}/results.html")
            elif args.existing_criteria_job_id:
                page.evaluate(
                    "jobId => localStorage.setItem('currentJobId', jobId)",
                    args.existing_criteria_job_id,
                )
                wait_for_network(page, f"{frontend_url}/criteria.html")
                page.locator("#criteriaBody tr").first.wait_for()
                assert page.locator("#criteriaBody input[type=text]").count() > 0
                page.locator("#confirmBtn").click()
                page.wait_for_url("**/results.html", timeout=600_000, wait_until="networkidle")
            else:
                page.locator("#fileInput").set_input_files(str(csv_path))
                page.locator("#roleSelectCard:not(.hidden)").wait_for()
                page.select_option("#roleSelect", label="后端工程师 (1 条)")
                page.locator("#actionBar:not(.hidden)").wait_for()
                assert "共 1 条记录" in page.locator("#fileInfo").inner_text()
                page.locator("#candidateLimit").fill("1")

                page.locator("#createTaskBtn").click()
                page.wait_for_url("**/criteria.html", timeout=180_000, wait_until="networkidle")
                page.locator("#criteriaBody tr").first.wait_for()
                assert page.locator("#criteriaBody input[type=text]").count() > 0

                page.locator("#confirmBtn").click()
                page.wait_for_url("**/results.html", timeout=600_000, wait_until="networkidle")

            for tab_index in range(3):
                page.locator(".tab").nth(tab_index).click()
                page.wait_for_function(
                    "() => !document.querySelector('#resultsBody').innerText.includes('正在从后端读取结果')"
                )
                if page.locator(".view-detail-btn").count() > 0:
                    break
            assert page.locator(".view-detail-btn").count() == 1
            count_notice = page.locator("#limitNotice").inner_text()
            assert "本轮选择 1 份" in count_notice
            assert "已处理 1 份 = 成功 1 份 + 失败 0 份" in count_notice

            page.locator(".view-detail-btn").first.click()
            page.wait_for_url("**/detail.html?id=*", timeout=30_000, wait_until="networkidle")
            page.locator("#candidateContent:not(.hidden)").wait_for()
            assert page.locator("#candId").inner_text() == "E2E-001"
            assert page.locator("#analysisBody tr").count() > 0
            assert "5年后端开发经验" in page.locator("#resumeText").inner_text()
            assert page.locator("#candMatchScore").inner_text().strip()
            assert page.locator("#candEvidenceConfidence").inner_text().strip()

            page.locator("#reviewReason").fill("端到端联调验证：证据与岗位条件一致。")
            page.locator("#submitReviewBtn").click()
            page.wait_for_url("**/results.html", timeout=30_000, wait_until="networkidle")
            page.screenshot(path=str(screenshot), full_page=True)
            browser.close()

    if console_errors:
        raise AssertionError("浏览器控制台错误：" + " | ".join(console_errors))
    print(f"E2E_OK screenshot={screenshot}")


if __name__ == "__main__":
    main()
