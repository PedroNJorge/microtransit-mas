"""src/agents/driver.py  DriverAgent skeleton (Milestone 6 adds bidding, trips, status)."""
from agents.base_agent import BaseAgent


class DriverAgent(BaseAgent):
    role = "driver"
    subscribes_to_traffic = True

    def __init__(self, jid: str, password: str, start_hub: str = "", **kwargs):
        super().__init__(jid, password, **kwargs)
        self.start_hub = start_hub            # hub id where this driver starts its shift
