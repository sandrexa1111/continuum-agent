"""A small, deterministic LangGraph app used across the adapter tests.

Plain Python node functions, no model, no network. The point of the adapter is
that Continuum can move LangGraph's *state*; proving that does not require an
LLM in the loop, and putting one there would make every assertion below flaky.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from continuum_langgraph import GraphBinding, LangGraphAdapter


class ReviewState(TypedDict):
    goal: str
    findings: Annotated[list[str], operator.add]
    stage: str
    scratch: dict


def scan(state: ReviewState) -> dict:
    return {
        "findings": ["auth-service.md", "billing.txt"],
        "stage": "extract",
        "scratch": {"scanned": 2},
    }


def extract(state: ReviewState) -> dict:
    return {"findings": ["FIXME insecure default credentials"], "stage": "rank"}


def rank(state: ReviewState) -> dict:
    return {"findings": ["ranked: FIXME insecure default credentials"], "stage": "done"}


def build_graph() -> StateGraph:
    builder = StateGraph(ReviewState)
    builder.add_node("scan", scan)
    builder.add_node("extract", extract)
    builder.add_node("rank", rank)
    builder.add_edge(START, "scan")
    builder.add_edge("scan", "extract")
    builder.add_edge("extract", "rank")
    builder.add_edge("rank", END)
    return builder


def build_different_graph() -> StateGraph:
    """Same channels, different topology -- used to test fingerprint refusal."""
    builder = StateGraph(ReviewState)
    builder.add_node("scan", scan)
    builder.add_node("rank", rank)
    builder.add_edge(START, "scan")
    builder.add_edge("scan", "rank")
    builder.add_edge("rank", END)
    return builder


BINDING = GraphBinding(
    goal_channel="goal",
    memory_channels=("findings",),
    pinned_channels=(),
)

INITIAL = {"goal": "review the corpus", "findings": [], "stage": "scan", "scratch": {}}

GRANTS = ["graph.execute"]


@pytest.fixture
def agent() -> LangGraphAdapter:
    adapter = LangGraphAdapter(build_graph, agent_id="lg-reviewer", thread_id="t1", binding=BINDING)
    adapter.start(dict(INITIAL))
    return adapter


@pytest.fixture
def midway(agent: LangGraphAdapter) -> LangGraphAdapter:
    """An adapter stopped between `extract` and `rank`."""
    agent.step()  # scan
    agent.step()  # extract
    return agent
