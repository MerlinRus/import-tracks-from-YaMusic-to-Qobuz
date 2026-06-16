import asyncio
import json
import os
import sys
import time
import urllib.parse


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def browser_channels():
    yield "chrome"
    yield "msedge"
    yield None


def main():
    if sys.platform.startswith("win") and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(json.dumps({
            "status": "error",
            "error": "Playwright не установлен. Запустите: pip install playwright && playwright install",
            "details": str(exc),
        }, ensure_ascii=False))
        return 1

    token = None
    captured_app_id = None
    context = None
    profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".qobuz_login_profile")

    try:
        with sync_playwright() as p:
            last_launch_error = None
            for channel in browser_channels():
                try:
                    launch_options = {
                        "headless": False,
                        "viewport": {"width": 1280, "height": 860},
                        "locale": "ru-RU",
                        "timezone_id": "Europe/Moscow",
                        "user_agent": USER_AGENT,
                        "args": [
                            "--disable-blink-features=AutomationControlled",
                            "--disable-infobars",
                            "--no-first-run",
                        ],
                        "ignore_default_args": ["--enable-automation"],
                    }
                    if channel:
                        launch_options["channel"] = channel

                    context = p.chromium.launch_persistent_context(profile_dir, **launch_options)
                    break
                except Exception as exc:
                    last_launch_error = exc
                    context = None

            if context is None:
                raise last_launch_error or RuntimeError("Не удалось запустить браузер")

            context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                """
            )

            page = context.new_page()

            def handle_request(request):
                nonlocal token, captured_app_id
                headers = request.headers
                if "x-user-auth-token" in headers:
                    token = headers["x-user-auth-token"]

                url = request.url
                if "api.json" in url:
                    parsed_url = urllib.parse.urlparse(url)
                    query_params = urllib.parse.parse_qs(parsed_url.query)
                    if "app_id" in query_params:
                        captured_app_id = query_params["app_id"][0]

            page.on("request", handle_request)
            page.goto("https://play.qobuz.com/login", wait_until="domcontentloaded")

            for _ in range(120):
                if token or page.is_closed():
                    break
                time.sleep(1)

    except Exception as exc:
        print(json.dumps({
            "status": "error",
            "error": str(exc),
        }, ensure_ascii=False))
        return 1
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass

    if not token:
        print(json.dumps({
            "status": "error",
            "error": "Не удалось войти в аккаунт или перехватить токен.",
        }, ensure_ascii=False))
        return 1

    print(json.dumps({
        "status": "success",
        "token": token,
        "app_id": captured_app_id,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
