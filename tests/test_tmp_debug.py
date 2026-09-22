
import asyncio
from datetime import datetime, timedelta
from core.living_state import LivingGate
from test_m6_patch1 import NOW

def test_debug_legacy_across(tmp_path):
    config = {
        "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0},
        "capabilities": {"cooldown_between_activities_hours": 0.0},
        "sleep": {
            "sleep_mode": "fixed",
            "sleep_window": "00:30-08:00",
            "wake_n_messages": 3,
            "sleep_mute_replies": True,
        },
    }
    async def flow():
        gate = LivingGate(config_getter=lambda: config,
                          db_path=str(tmp_path/"gate.db"), rng=lambda: 0.5)
        now = NOW
        await gate.enter_autonomous_sleep(now + timedelta(hours=5), "long", now)
        sleeping = await gate.should_wake(now + timedelta(minutes=20))
        across = await gate.should_wake(now + timedelta(hours=4))
        print("ACROSS:", across, "asleep:", gate.asleep_in_autonomous(now + timedelta(hours=4)))
        await gate.close()
        return across
    across = asyncio.run(flow())
    print("RESULT:", across)
    assert across == (False, "sleeping")
