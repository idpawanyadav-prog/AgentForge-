import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.task_registry import BackgroundTaskRegistry  # noqa: E402


def test_create_runs_and_tracks_task():
    async def drive():
        reg = BackgroundTaskRegistry()
        ran = asyncio.Event()

        async def job():
            ran.set()

        task = reg.create(job(), name="job")
        assert reg.count == 1
        await ran.wait()
        await asyncio.sleep(0)          # let the done-callback fire
        assert reg.count == 0           # finished tasks are discarded
    asyncio.run(drive())


def test_failure_is_not_silently_swallowed():
    async def drive():
        reg = BackgroundTaskRegistry()

        async def boom():
            raise ValueError("kaboom")

        task = reg.create(boom(), name="boom")
        with_raises = False
        try:
            await task
        except ValueError:
            with_raises = True
        assert with_raises
        assert reg.count == 0
    asyncio.run(drive())


def test_cancel_all_cancels_pending():
    async def drive():
        reg = BackgroundTaskRegistry()

        async def forever():
            await asyncio.sleep(3600)

        tasks = [reg.create(forever(), name=f"t{i}") for i in range(2)]
        assert reg.count == 2
        assert reg.cancel_all() == 2
        await asyncio.sleep(0)          # let the cancellations settle
        assert reg.count == 0
        for t in tasks:
            assert t.cancelled() or t.done()
    asyncio.run(drive())


def test_create_from_worker_thread_uses_captured_loop():
    """create() must schedule onto the captured main loop from a worker thread."""
    import threading

    reg = BackgroundTaskRegistry()
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        reg.set_loop(loop)
        box = {}

        def worker():
            async def job():
                return "on-main"
            box["fut"] = reg.create(job(), name="w")

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2)
        assert box["fut"].result(timeout=2) == "on-main"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=2)
        loop.close()

