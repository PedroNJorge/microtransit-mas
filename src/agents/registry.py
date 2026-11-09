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
