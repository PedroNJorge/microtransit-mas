"""src/agents/registry.py  Role name -> agent class (used by the launcher)."""
from agents.driver import DriverAgent
from agents.environment import EnvironmentAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.regulator import RegulatorAgent
from agents.reputation import ReputationAgent
from agents.rider import RiderAgent

AGENT_CLASSES = {cls.role: cls for cls in (
    RiderAgent, DriverAgent, MicrohubAgent, PaymentAgent,
    RegulatorAgent, ReputationAgent, EnvironmentAgent)}

try:                                                    # the BDI driver brain needs the optional `spade_bdi` package
    from agents.brain import DriverBrain
    AGENT_CLASSES[DriverBrain.role] = DriverBrain
except ImportError:                                     # scenarios with "brains": true then fail with a clear error
    pass
