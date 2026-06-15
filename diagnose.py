import asyncio
from src.config import load_config
from src.queue import URLQueue

async def main():
    config = load_config()
    queue = URLQueue(config.queue)
    await queue.load_state()
    print("Pending count before enqueue:", queue.pending_count)
    added = await queue.enqueue(["https://www.amazon.in/dp/B07XJ8C8F2"])
    print("Added:", added)
    print("Pending count after enqueue:", queue.pending_count)
    batch = await queue.dequeue(10)
    print("Dequeued batch size:", len(batch))
    print("Dequeued batch:", batch)
    print("Pending count after dequeue:", queue.pending_count)

asyncio.run(main())
