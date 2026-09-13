"""
Ephemeral Agent Launcher

Spawns short-lived agent tasks at configurable churn rates.
"""
import asyncio
import os
import random
import time
from typing import List, Callable
from dataclasses import dataclass


_ARRIVAL_RNG = random.Random(int(os.getenv("ARRIVAL_SEED", "20260514")))


def _next_arrival_delta(rate: float) -> float:
    """Inter-arrival sleep given the chosen distribution.

    ARRIVAL_DIST=poisson uses exponential gaps with mean 1/rate (i.e.
    Poisson arrival process at the requested mean rate). Default is the
    deterministic 1/rate gap retained for back-compatibility with the
    headline cells in the paper.
    """
    if os.getenv("ARRIVAL_DIST", "deterministic").lower() == "poisson":
        return _ARRIVAL_RNG.expovariate(rate)
    return 1.0 / rate


@dataclass
class AgentTask:
    """Represents an ephemeral agent task"""
    agent_id: str
    spawn_time: float
    requests_to_make: int = 1


class AgentLauncher:
    """
    Launches ephemeral agents at a specified churn rate.
    """
    
    def __init__(self, churn_rate: int, duration: int = 60):
        """
        Initialize launcher.
        
        Args:
            churn_rate: Number of new agents per second (1, 5, or 20)
            duration: Total duration in seconds
        """
        self.churn_rate = churn_rate
        self.duration = duration
        self.agent_counter = 0
    
    async def spawn_agent(
        self,
        agent_task: AgentTask,
        request_func: Callable
    ) -> List[dict]:
        """
        Spawn a single ephemeral agent and execute requests.
        
        Args:
            agent_task: Agent task configuration
            request_func: Function to execute requests (returns RequestResult)
            
        Returns:
            List of request results
        """
        results = []
        
        for i in range(agent_task.requests_to_make):
            result = await asyncio.to_thread(request_func)
            results.append({
                "agent_id": agent_task.agent_id,
                "request_num": i + 1,
                "spawn_time": agent_task.spawn_time,
                "result": result
            })
        
        return results
    
    async def run(
        self,
        request_func: Callable,
        warmup: int = 10
    ) -> List[dict]:
        """
        Run the launcher for the specified duration.
        
        Args:
            request_func: Function that makes a single API request
            warmup: Warmup period in seconds before recording results
            
        Returns:
            List of all request results
        """
        all_results = []
        start_time = time.time()
        recording_start = start_time + warmup
        
        # Track next spawn time; first delta is sampled from the
        # configured arrival distribution (deterministic by default,
        # poisson when ARRIVAL_DIST=poisson).
        next_spawn = start_time + _next_arrival_delta(self.churn_rate)
        
        # Active agent tasks
        active_tasks = []
        
        print(f"Starting launcher: churn_rate={self.churn_rate}/s, "
              f"warmup={warmup}s, duration={self.duration}s")
        
        while time.time() < start_time + warmup + self.duration:
            current_time = time.time()
            
            # Spawn new agents if it's time
            if current_time >= next_spawn:
                self.agent_counter += 1
                agent_task = AgentTask(
                    agent_id=f"agent-{self.agent_counter}",
                    spawn_time=current_time,
                    requests_to_make=1  # Each agent makes 1 request
                )
                
                # Launch agent task
                task = asyncio.create_task(
                    self.spawn_agent(agent_task, request_func)
                )
                active_tasks.append((task, current_time >= recording_start))
                
                # Schedule next spawn
                next_spawn += _next_arrival_delta(self.churn_rate)
            
            # Collect completed tasks
            done_tasks = []
            for task, should_record in active_tasks:
                if task.done():
                    try:
                        results = await task
                        if should_record:
                            all_results.extend(results)
                    except Exception as e:
                        print(f"Agent task failed: {e}")
                    done_tasks.append((task, should_record))
            
            # Remove completed tasks
            for done_task in done_tasks:
                active_tasks.remove(done_task)
            
            # Small sleep to prevent busy-waiting
            await asyncio.sleep(0.01)
        
        # Wait for remaining tasks to complete
        for task, should_record in active_tasks:
            try:
                results = await task
                if should_record:
                    all_results.extend(results)
            except Exception as e:
                print(f"Agent task failed: {e}")
        
        print(f"Launcher complete: {len(all_results)} requests recorded")
        return all_results


async def run_launcher_test(
    mode: str,
    cache_enabled: bool,
    churn_rate: int,
    delegation_depth: int,
    duration: int = 60,
    warmup: int = 10
) -> List[dict]:
    """
    Run a launcher test with specified configuration.
    
    Args:
        mode: Authentication mode ('A', 'B', or 'C')
        cache_enabled: Whether token caching is enabled
        churn_rate: Agents per second
        delegation_depth: Delegation depth (1 or 2)
        duration: Test duration in seconds
        warmup: Warmup period in seconds
        
    Returns:
        List of request results
    """
    from agent.agent_client import AgentClient
    from benchmark.workload import workload_from_env

    # Create agent client
    client = AgentClient(
        mode=mode,
        cache_enabled=cache_enabled,
        delegation_depth=delegation_depth
    )

    # The workload generator picks its mode from WORKLOAD_MODE
    # (default: synthetic). For synthetic the returned WorkloadResult
    # forwards every attribute the launcher and CSV writer read from
    # the old RequestResult, so this is a drop-in replacement.
    workload = workload_from_env(client)

    def make_request():
        return workload.do_one()

    # Create and run launcher
    launcher = AgentLauncher(churn_rate=churn_rate, duration=duration)
    results = await launcher.run(make_request, warmup=warmup)

    # Close client
    client.close()

    return results


if __name__ == "__main__":
    # Example usage
    async def main():
        results = await run_launcher_test(
            mode="A",
            cache_enabled=False,
            churn_rate=5,
            delegation_depth=1,
            duration=10,
            warmup=2
        )
        print(f"Collected {len(results)} results")
        for r in results[:5]:
            print(f"  Agent {r['agent_id']}: "
                  f"latency={r['result'].latency_ms:.2f}ms, "
                  f"success={r['result'].success}")
    
    asyncio.run(main())
