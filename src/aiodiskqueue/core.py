"""Core implementation of a persistent AsyncIO queue."""
import asyncio
import io
import logging
import pickle
from pathlib import Path
from typing import Any, Union

import aiofiles
import aiofiles.os

from aiodiskqueue.exceptions import QueueEmpty, QueueFull
from aiodiskqueue.utils import NoDirectInstantiation

logger = logging.getLogger(__name__)


class Queue(metaclass=NoDirectInstantiation):
    """A persistent AsyncIO FIFO queue.

    The content of a queue is stored on disk in a data file.
    This file only exists temporarily while there are items in the queue.

    This class is not thread safe.

    To create a new object the factory method :func:`create` must be used.
    """

    def __init__(self, data_path: Path, maxsize: int, queue: list) -> None:
        """Direct instantiation would break the persistance feature
        and has therefore been disabled.

        :meta private:
        """
        self._data_path = Path(data_path)
        self._maxsize = max(0, maxsize)
        self._queue_lock = asyncio.Lock()
        self._has_new_item = asyncio.Condition()
        self._has_free_slots = asyncio.Condition()
        self._tasks_are_finished = asyncio.Condition()
        self._unfinished_tasks = 0
        self._peak_size = len(queue)  # measuring peak size of the queue
        self._queue = list(queue)

    @property
    def maxsize(self) -> int:
        """Number of items allowed in the queue. 0 means unlimited."""
        return self._maxsize

    def empty(self) -> bool:
        """Return True if the queue is empty, False otherwise.

        If empty() returns False it doesn't guarantee
        that a subsequent call to get() will not raise :class:`.QueueEmpty`.
        """
        return self.qsize() == 0

    def full(self) -> bool:
        """Return True if there are maxsize items in the queue.

        If the queue was initialized with maxsize=0 (the default),
        then full() always returns False.
        """
        if not self._maxsize:
            return False
        return self.qsize() >= self._maxsize

    async def get(self) -> Any:
        """Remove and return an item from the queue. If queue is empty,
        wait until an item is available.
        """
        while True:
            try:
                return await self.get_nowait()
            except QueueEmpty:
                pass
            async with self._has_new_item:
                await self._has_new_item.wait()

    async def get_nowait(self) -> Any:
        """Remove and return an item if one is immediately available,
        else raise :class:`.QueueEmpty`.
        """
        async with self._queue_lock:
            if not self._queue:
                raise QueueEmpty()

            item = self._queue.pop(0)
            if self._queue:
                await self._write_queue()
            else:
                await aiofiles.os.remove(self._data_path)

        if self._maxsize:
            async with self._has_free_slots:
                self._has_free_slots.notify()

        return item

    async def join(self) -> None:
        """Block until all items in the queue have been gotten and processed.

        The count of unfinished tasks goes up whenever an item is added to the
        queue. The count goes down whenever a consumer calls task_done() to
        indicate that the item was retrieved and all work on it is complete.
        When the count of unfinished tasks drops to zero, join() unblocks.
        """
        async with self._tasks_are_finished:
            if self._unfinished_tasks > 0:
                await self._tasks_are_finished.wait()

    async def put(self, item: Any) -> None:
        """Put an item into the queue. If the queue is full,
        wait until a free slot is available before adding the item.
        """
        while True:
            try:
                return await self.put_nowait(item)
            except QueueFull:
                pass

            async with self._has_free_slots:
                await self._has_free_slots.wait()

    async def put_nowait(self, item: Any) -> None:
        """Put an item into the queue without blocking.

        If no free slot is immediately available, raise :class:`.QueueFull`.

        Args:
            item: Any Python object that can be pickled
        """
        async with self._queue_lock:
            if self._maxsize and self.qsize() >= self._maxsize:
                raise QueueFull
            self._queue.append(item)
            await self._append_item_to_file(self._data_path, item)
            async with self._tasks_are_finished:
                self._unfinished_tasks += 1

        async with self._has_new_item:
            self._has_new_item.notify()

    def qsize(self) -> int:
        """Return the approximate size of the queue.
        Note, qsize() > 0 doesn't guarantee that a subsequent get()
        will not raise :class:`.QueueEmpty`.
        """
        return len(self._queue)

    async def task_done(self) -> None:
        """Indicate that a formerly enqueued task is complete.

        Used by queue consumers. For each get() used to fetch a task,
        a subsequent call to task_done() tells the queue that the processing
        on the task is complete.

        If a join() is currently blocking, it will resume when all items have
        been processed (meaning that a task_done() call was received for every
        item that had been put() into the queue).

        Raises ValueError if called more times than there were items placed in
        the queue.
        """
        async with self._tasks_are_finished:
            if self._unfinished_tasks <= 0:
                raise ValueError("task_done() called too many times")

            self._unfinished_tasks -= 1
            if self._unfinished_tasks == 0:
                self._tasks_are_finished.notify_all()

    async def _write_queue(self):
        await self._write_objs_to_file(self._data_path, self._queue)
        size = self.qsize()
        self._peak_size = max(self._peak_size, size)
        logger.debug("Wrote queue with %d items: %s", size, self._data_path)

    @staticmethod
    async def _write_objs_to_file(data_path: Path, items: list):
        with io.BytesIO() as buffer:
            for item in items:
                pickle.dump(item, buffer)
            buffer.seek(0)
            data = buffer.read()
        async with aiofiles.open(data_path, "wb", buffering=0) as f:
            await f.write(data)

    @staticmethod
    async def _append_item_to_file(data_path: Path, item: Any):
        async with aiofiles.open(data_path, "ab", buffering=0) as fp:
            await fp.write(pickle.dumps(item))

    @classmethod
    async def _read_items_from_file(cls, data_path: Path) -> list:
        try:
            async with aiofiles.open(data_path, "rb", buffering=0) as f:
                pickled_bytes = await f.read()
        except FileNotFoundError:
            return []

        items = []
        with io.BytesIO() as buffer:
            buffer.write(pickled_bytes)
            buffer.seek(0)
            while True:
                try:
                    item = pickle.load(buffer)
                except EOFError:
                    break
                except pickle.PickleError:
                    backup_path = data_path.with_suffix(".bak")
                    await aiofiles.os.rename(data_path, backup_path)
                    logger.exception(
                        "Data file is corrupt and has been backed up: %s", backup_path
                    )
                    return []
                else:
                    items.append(item)

        logger.info(
            "Fetching %d items from existing data file: %s", len(items), data_path
        )
        return items

    @classmethod
    async def create(cls, data_path: Union[str, Path], maxsize: int = 0) -> "Queue":
        """Create a new queue instance.

        A data file will be created at the given path if it does not already exist.

        If the data file already exists, if will be used to recreate the queue if possible.
        Should that fail, the existing data file will be backed up and the queue reset.

        Please note that using two different instances with the same data file
        simultaneously is not recommended as it may lead to data corruption.

        Args:
            data_path: Path of the data file for this queue. e.g. `queue.dat`
            maxsize: If maxsize is less than or equal to zero, the queue size is infinite.
                If it is an integer greater than 0, then put() blocks
                when the queue reaches maxsize until an item is removed by get().
        """
        data_path = Path(data_path)
        if data_path.suffix == ".bak":
            raise ValueError("Invalid file name: .bak suffix is reserved for backups")
        queue = await cls._read_items_from_file(data_path)
        if not queue:
            await cls._write_objs_to_file(data_path, [])  # ensuring early we can write
            await aiofiles.os.remove(data_path)

        return cls._create(data_path, maxsize, queue)
