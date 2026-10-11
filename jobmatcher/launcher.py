"""
Launcher — starts both the Telegram bot and the Web Dashboard in parallel.
Used as the Docker CMD entrypoint.
"""

import sys
import threading
import time


def start_telegram_bot():
    """Start the Telegram bot (blocking polling loop)."""
    import job_matcher

    print("🤖 Starting Telegram Bot...")
    try:
        job_matcher.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    backoff = 10
    while True:
        try:
            job_matcher.bot.polling(non_stop=True, timeout=60, long_polling_timeout=30, allowed_updates=["message"])
            backoff = 10  # reset on clean exit
        except Exception as e:
            err_str = str(e)
            print(f"⚠️ Telegram polling exception: {err_str}")
            if "NameResolutionError" in err_str or "Temporary failure in name resolution" in err_str:
                print("💥 CRITICAL DNS FAILURE. Exiting to allow Docker auto-heal restart.")
                import os
                os._exit(1)
            print(f"🔄 Reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def start_web_dashboard():
    """Start the Flask web dashboard."""
    import web_server
    web_server.start_web_server()


def main():
    # Check if this is a one-shot cron scrape
    if len(sys.argv) > 1 and sys.argv[1] == "--scrape-now":
        import job_matcher
        print("🤖 Running on-demand scrape via cron...")
        job_matcher.run_job_matcher()
        print("🤖 Cron scrape complete. Exiting.")
        sys.exit(0)

    print("=" * 60)
    print("🚀 Job Matcher — Starting All Services")
    print("=" * 60)

    # Start web dashboard in a background thread
    web_thread = threading.Thread(target=start_web_dashboard, daemon=True)
    web_thread.start()

    # Give Flask a second to bind
    time.sleep(1)

    from job_retention import retention_worker
    threading.Thread(target=retention_worker, args=(threading.Event(),),
                     name='job-retention', daemon=True).start()

    # Run Telegram bot in the main thread (blocking)
    import job_matcher
    if job_matcher.bot is None:
        print("Telegram is disabled; running the dashboard only.")
        web_thread.join()
    else:
        start_telegram_bot()


if __name__ == "__main__":
    main()
