import asyncio
import inspect
from asyncio import Task
from datetime import datetime, timedelta
from random import randint
from typing import Optional, Dict

from models.config import Config
from models.event import Event, update_events, action_event, check_event_diff
from models.facility import Facility
from models.usercontext import UserContext
from modules.console_util import print_events
from modules.math_util import percentage_of


class MyWellnezz:
    def __init__(self):
        self.print_task: Optional[Task] = None
        self.book_tasks: Dict[str, Task] = {}
        self.events: Dict[str, Event] = {}
        self.lock_events = asyncio.Lock()
        self.lock_tasks = asyncio.Lock()
        self.long_cycle = 60 * 10
        self.small_cycle = 15
        self.cycle_timeout = self.long_cycle
        self.cycle_iteration = 1
        self.run = True
        self.test = False

    async def get_events(self) -> Dict[str, Event]:
        async with self.lock_events:
            return self.events

    async def get_event(self, idx: str) -> Event:
        async with self.lock_events:
            return self.events[idx]

    async def set_events(self, user: UserContext, facility: Facility) -> Dict[str, Event]:
        d = int((datetime.now()).strftime("%Y%m%d"))
        async with self.lock_events:
            self.events = {k: v for k, v in (await update_events(user, facility, d)).items() if not v.is_ended()}
            return self.events

    async def get_event_id_by_index(self, index: int) -> Optional[str]:
        ev = await self.get_events()
        return next((key for i, key in enumerate(ev.keys()) if i == abs(index)), None)

    async def set_event_status(self, idx: str, status: str) -> None:
        (await self.get_event(idx)).status = status

    async def get_book_tasks(self) -> Dict[str, Task]:
        async with self.lock_tasks:
            return self.book_tasks

    async def get_book_task(self, idx: str) -> Optional[Task]:
        async with self.lock_tasks:
            return self.book_tasks[idx] if idx in self.book_tasks else None

    async def pop_book_task(self, idx: str) -> None:
        async with self.lock_tasks:
            self.book_tasks.pop(idx)

    async def set_book_task(self, user: UserContext, facility: Facility, event: Event):
        if event.is_started():
            print(f'{event.name} is already started')
            return
        if event.id in self.book_tasks and not (await self.get_book_task(event.id)).done():
            (await self.get_book_task(event.id)).cancel()
            await self.pop_book_task(event.id)
            await self.set_event_status(event.id, event.get_status())
            return
        self.book_tasks[event.id] = asyncio.create_task(self._book_event_loop(user, facility, event))

    def set_event_task(self, user: UserContext, facility: Facility, config: Config):
        if self.print_task is None or self.print_task.done():
            self.print_task = asyncio.create_task(self._events_loop(user, facility, config))

    async def _book_event_loop(self, user: UserContext, facility: Facility, event: Event):
        while self.run:
            try:
                event = await self.get_event(event.id)
            except Exception as ex:
                print(f'Event not found: {ex}')
            if not event or event.is_ended() or event.is_started() or not event.is_bookable():
                break
            elif event.available_places > 0 or event.is_participant:
                if user.token is None or not user.token:
                    await user.refresh()
                if await action_event(user, event):
                    break
            await asyncio.sleep(2)
        await asyncio.sleep(2)
        n_events = await self.set_events(user, facility)
        await self.set_loops_timeout(n_events)

    async def update_events_event(self, user: UserContext, facility: Facility, config: Config, old_events: []):
        new_events = await self.set_events(user, facility)
        if config.auto_book:
            events_diff = check_event_diff(new_events, old_events)
            for e in events_diff:
                await self.set_book_task(user, facility, await self.get_event(e))
        await self.set_loops_timeout(new_events)
        return new_events

    async def _events_loop(self, user: UserContext, facility: Facility, config: Config):
        events = []
        while self.run:
            try:
                await self.clean_tasks()
                if len(events) == 0:
                    events = await self.update_events_event(user, facility, config, events)
                elif await print_events(facility, user, await self.get_events(), self.cycle_iteration,
                                        self.cycle_timeout):
                    n_events = await self.update_events_event(user, facility, config, events)
                    events = n_events
                self.cycle_iteration += 1
                await asyncio.sleep(1)
            except Exception as e:
                print(f'Error in {inspect.currentframe().f_code.co_name}: {e}')

    async def set_loops_timeout(self, events: Dict[str, Event]):
        tasks = await self.get_book_tasks()
        cycle = self.long_cycle
        if len(tasks) > 0:
            task_events = {x: events[x] for x in events if x in tasks}
            for event in task_events.values():
                if (event.booking_opens_on - timedelta(minutes=30)) < datetime.now():
                    cycle = self.small_cycle
                    break
        x = percentage_of(cycle, 20)
        self.cycle_timeout = cycle + randint(-x, x)
        self.cycle_iteration = 1

    async def clean_tasks(self):
        try:
            tasks = (await self.get_book_tasks()).copy()
            for t in tasks:
                bt = await self.get_book_task(t)
                if bt and bt.done():
                    await self.pop_book_task(t)
                else:
                    event = await self.get_event(t)
                    if not event.is_participant and event.status != 'Booking':
                        await self.set_event_status(event.id, 'Booking')
        except Exception as e:
            print(f'Error in {inspect.currentframe().f_code.co_name}: {e}')
