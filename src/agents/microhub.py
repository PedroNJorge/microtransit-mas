"""src/agents/microhub.py  MicrohubAgent skeleton (Milestone 5 adds pooling and auctions)."""
from agents.base_agent import BaseAgent


class MicrohubAgent(BaseAgent):
    role = "microhub"
    subscribes_to_traffic = True

    def __init__(self, jid: str, password: str, hub_id: str = "", **kwargs):
        super().__init__(jid, password, **kwargs)
        self.hub_id = hub_id                  # node id in the road network (== MicrohubNode.id)
