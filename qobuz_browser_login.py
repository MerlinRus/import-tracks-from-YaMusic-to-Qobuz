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


def build_browser_env(profile_dir):
    browser_env = os.environ.copy()
    if sys.platform.startswith("win"):
        return browser_env

    runtime_root = os.getenv("QSYNC_BROWSER_RUNTIME_DIR")
    if not runtime_root:
        profiles_root = os.path.dirname(os.path.abspath(profile_dir))
        runtime_root = os.path.dirname(profiles_root)

    browser_env["HOME"] = runtime_root
    browser_env.setdefault("XDG_CACHE_HOME", os.path.join(runtime_root, ".cache"))
    browser_env.setdefault("XDG_CONFIG_HOME", os.path.join(runtime_root, ".config"))
    browser_env.setdefault("XDG_DATA_HOME", os.path.join(runtime_root, ".local", "share"))

    for key in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        os.makedirs(browser_env[key], mode=0o700, exist_ok=True)

    return browser_env


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
    profile_dir = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".qobuz_login_profile")
    )
    browser_env = build_browser_env(profile_dir)

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
                        "env": browser_env,
                        "args": [
                            "--disable-blink-features=AutomationControlled",
                            "--disable-breakpad",
                            "--disable-crashpad",
                            "--disable-crash-reporter",
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
