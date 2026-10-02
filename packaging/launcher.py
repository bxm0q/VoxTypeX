from multiprocessing import freeze_support

if __name__ == "__main__":
    # The Whisper child must enter spawn_main before Qt creates a second tray.
    freeze_support()

    import sys

    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--smoke-test":
            from frozen_smoke import run

            exit_code = run(sys.argv[2], sys.argv[3])
        else:
            from voxtypex.main import main

            exit_code = main()
    except Exception:
        import ctypes
        import logging
        import os
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        directory = Path(os.environ.get("VOXTYPEX_DATA_DIR", Path(os.environ["LOCALAPPDATA"]) / "VoxTypeX"))
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "startup-error.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8"
        )
        logger = logging.getLogger("voxtypex.launcher")
        logger.addHandler(handler)
        logger.exception("Packaged application startup failed")
        handler.close()
        ctypes.windll.user32.MessageBoxW(
            None,
            f"Не удалось запустить VoxTypeX. Подробности: {directory / 'startup-error.log'}",
            "VoxTypeX",
            16,
        )
        exit_code = 1
    raise SystemExit(exit_code)
