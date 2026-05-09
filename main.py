# =====================================
# WEB SERVER & MAIN RUNNER
# =====================================

async def init_web_app():
    webapp = web.Application()

    async def health_check(request):
        return web.Response(text="Bot is running smoothly.")

    webapp.router.add_get("/", health_check)

    runner = web.AppRunner(webapp)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logger.info(f"Web server active on port {PORT}")


async def main():

    logger.info("Connecting MongoDB...")

    await create_indexes()

    logger.info("MongoDB Connected")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # COMMANDS

    application.add_handler(
        CommandHandler(
            "start",
            start_cmd
        )
    )

    application.add_handler(
        CommandHandler(
            "addchannel",
            add_channel_cmd
        )
    )

    # MESSAGE HANDLER

    application.add_handler(
        MessageHandler(
            (
                filters.TEXT
                |
                filters.PHOTO
            )
            &
            ~filters.COMMAND,
            message_handler
        )
    )

    # AUTO POST SCHEDULER

    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        auto_post_job,
        "interval",
        minutes=30,
        args=[application],
        max_instances=1
    )

    scheduler.start()

    logger.info("Auto post engine synchronized.")

    # START WEB SERVER

    await init_web_app()

    # START BOT

    await application.initialize()

    await application.start()

    await application.updater.start_polling()

    logger.info("Bot streaming updates active.")

    while True:
        await asyncio.sleep(3600)


# =====================================
# RUN
# =====================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):

        logger.info("System Offline.")
