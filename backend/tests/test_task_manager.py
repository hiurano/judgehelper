import asyncio

from backend.services.task_manager import cancel_all, cancel_job, spawn


def test_cancel_job_stops_only_its_submission_and_drafting():
    async def run():
        cleanup = []
        async def worker(name):
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.append(name)
        own = [spawn(worker(kind), name=f'{kind}:job-cancel') for kind in ('aai-submit', 'process', 'recover')]
        other = spawn(worker('other'), name='process:job-other')
        await asyncio.sleep(0)
        await cancel_job('job-cancel')
        assert all(task.cancelled() for task in own)
        assert not other.done()
        assert set(cleanup) == {'aai-submit', 'process', 'recover'}
        await cancel_all()
    asyncio.run(run())
