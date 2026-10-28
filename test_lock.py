"""Single-instance advisory lock: re-checking it and stopping the workers."""
import asyncio
import unittest

import main
from booking_store import WorkerLock


class FakeConnection:
    """Enough of an asyncpg connection for the lock helpers."""

    def __init__(self, pid=4242, taken=True):
        self.pid = pid
        self.taken = taken
        self.dead = False
        self.released = False

    async def fetchval(self, query, *args):
        if self.dead:
            raise RuntimeError('connection is closed')
        if 'pg_try_advisory_lock' in query:
            return self.taken
        if 'pg_backend_pid' in query:
            return self.pid
        if 'pg_advisory_unlock' in query:
            return True
        raise AssertionError('unexpected query: ' + query)


class FakePool:
    def __init__(self, connection=None, locks=1):
        self.connection = connection or FakeConnection()
        self.locks = locks
        self.acquired = 0
        self.released = 0

    async def acquire(self):
        self.acquired += 1
        return self.connection

    async def release(self, connection):
        self.released += 1
        connection.released = True

    async def fetchrow(self, query, *args):
        return {'n': self.locks}


class Acquire(unittest.IsolatedAsyncioTestCase):
    async def test_takes_the_lock(self):
        pool = FakePool()
        lock = WorkerLock(pool, 7319846)
        self.assertTrue(await lock.acquire())
        self.assertEqual(lock.pid, 4242)

    async def test_refuses_when_another_instance_owns_it(self):
        pool = FakePool(FakeConnection(taken=False))
        lock = WorkerLock(pool, 7319846)
        self.assertFalse(await lock.acquire())
        self.assertIsNone(lock.conn)
        self.assertEqual(pool.released, 1)

    async def test_releases_the_lock(self):
        pool = FakePool()
        lock = WorkerLock(pool, 7319846)
        await lock.acquire()
        await lock.release()
        self.assertIsNone(lock.conn)
        self.assertTrue(pool.connection.released)


class Holds(unittest.IsolatedAsyncioTestCase):
    async def test_held_while_the_session_owns_it(self):
        lock = WorkerLock(FakePool(locks=1), 7319846)
        await lock.acquire()
        self.assertTrue(await lock.holds())

    async def test_lost_when_the_lock_is_not_listed(self):
        lock = WorkerLock(FakePool(locks=0), 7319846)
        await lock.acquire()
        self.assertFalse(await lock.holds())

    async def test_lost_when_the_connection_died(self):
        pool = FakePool()
        lock = WorkerLock(pool, 7319846)
        await lock.acquire()
        pool.connection.dead = True
        self.assertFalse(await lock.holds())

    async def test_lost_when_the_connection_was_replaced(self):
        pool = FakePool()
        lock = WorkerLock(pool, 7319846)
        await lock.acquire()
        pool.connection.pid = 777
        self.assertFalse(await lock.holds())

    async def test_lost_before_acquire(self):
        lock = WorkerLock(FakePool(), 7319846)
        self.assertFalse(await lock.holds())


class Watchdog(unittest.IsolatedAsyncioTestCase):
    async def test_calls_back_when_the_lock_is_lost(self):
        lost = []

        class Lock:
            calls = 0

            async def holds(self):
                Lock.calls += 1
                return Lock.calls < 2

        main.LOCK_CHECK_INTERVAL = 0.01
        try:
            await asyncio.wait_for(
                main.lock_watchdog(Lock(), lambda: lost.append(True) or asyncio.sleep(0),
                                   retries=0, retry_delay=0), 2)
        finally:
            main.LOCK_CHECK_INTERVAL = 60
        self.assertEqual(lost, [True])

    async def test_retries_before_giving_up(self):
        lost = []
        checks = []

        class Lock:
            async def holds(self):
                checks.append(True)
                return False

        main.LOCK_CHECK_INTERVAL = 0.01
        try:
            await asyncio.wait_for(
                main.lock_watchdog(Lock(), lambda: lost.append(True) or asyncio.sleep(0),
                                   retries=2, retry_delay=0), 2)
        finally:
            main.LOCK_CHECK_INTERVAL = 60
        self.assertEqual(lost, [True])
        self.assertEqual(len(checks), 3)

    async def test_keeps_going_while_the_lock_is_held(self):
        lost = []

        class Lock:
            async def holds(self):
                return True

        main.LOCK_CHECK_INTERVAL = 0.01
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(main.lock_watchdog(Lock(), lambda: lost.append(True) or asyncio.sleep(0)), 0.1)
        finally:
            main.LOCK_CHECK_INTERVAL = 60
        self.assertEqual(lost, [])


if __name__ == '__main__':
    unittest.main()
