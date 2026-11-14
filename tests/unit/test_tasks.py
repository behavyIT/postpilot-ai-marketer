from taskiq import InMemoryBroker

from app.workers.broker import broker
from app.workers.tasks.system import ping


def test_test_environment_uses_in_memory_broker() -> None:
    assert isinstance(broker, InMemoryBroker)


async def test_ping_task_runs_through_broker() -> None:
    task = await ping.kiq("проверка")
    result = await task.wait_result(timeout=5)
    assert not result.is_err
    assert result.return_value == "pong: проверка"
