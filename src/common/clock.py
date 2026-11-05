"""
src/common/clock.py
Gestão do tempo da simulação acelerada baseada em asyncio.
"""
import asyncio
import time

# Fator de aceleração da simulação
SIMULATION_SPEED: float = 10.0

# Registo da hora de início real (wall-clock time em segundos)
_START_WALL_TIME: float = time.time()


def set_simulation_speed(speed: float) -> None:
    """Ajusta o fator de aceleração da simulação."""
    global SIMULATION_SPEED
    if speed <= 0:
        raise ValueError("SIMULATION_SPEED deve ser estritamente positiva.")
    SIMULATION_SPEED = speed


def reset_clock() -> None:
    """Reinicia o relógio global da simulação para t=0."""
    global _START_WALL_TIME
    _START_WALL_TIME = time.time()


def now() -> float:
    """
    Devolve o tempo atual da simulação (em segundos simulados) 
    decorrido desde o arranque do sistema (t = 0.0).
    """
    elapsed_wall_seconds = time.time() - _START_WALL_TIME
    return elapsed_wall_seconds * SIMULATION_SPEED


async def sim_sleep(sim_seconds: float) -> None:
    """
    Substituto assíncrono do asyncio.sleep.
    Faz a pausa na execução aguardando o equivalente em tempo real 
    ao valor de sim_seconds simulados.
    """
    if sim_seconds <= 0:
        return
    real_seconds = sim_seconds / SIMULATION_SPEED
    await asyncio.sleep(real_seconds)