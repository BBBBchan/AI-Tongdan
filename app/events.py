"""Bounded, reconnectable event fan-out. Slow browsers never block ingestion."""
import asyncio
import copy
from collections import deque
import time


class EventBus:
    def __init__(self, history_size=512, queue_size=128):
        self.history = deque(maxlen=history_size)
        self.subscribers = set()
        self.sequence = 0
        self.queue_size = queue_size

    async def emit(self, event_type, data, session_id=None):
        self.sequence += 1
        item = {"id": self.sequence, "type": event_type, "data": copy.deepcopy(data),
                "session_id": session_id, "time": time.time()}
        self.history.append(item)
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)
        return item

    def subscribe(self, after=0):
        queue = asyncio.Queue(self.queue_size)
        if after:
            for item in self.history:
                if item["id"] > after:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(item)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)
