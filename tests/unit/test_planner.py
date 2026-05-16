from __future__ import annotations

from postcheck.core.planner import DEFAULT_PROBES, ExecutionPlan, RoutePlan, plan
from postcheck.core.types import AffectedRoute


def _route(path: str) -> AffectedRoute:
    return AffectedRoute(
        route=path,
        reason="direct",
        confidence="high",
    )


def test_plan_is_empty_for_empty_routes() -> None:
    p = plan([])
    assert isinstance(p, ExecutionPlan)
    assert p.routes == []
    assert p.parallel is False


def test_plan_assigns_default_probes_to_every_route() -> None:
    routes = [_route("/a"), _route("/b")]
    p = plan(routes)
    assert [rp.route.route for rp in p.routes] == ["/a", "/b"]
    for rp in p.routes:
        assert rp.probes == list(DEFAULT_PROBES)
        assert rp.probes == ["runtime", "network", "storage", "ui"]


def test_plan_preserves_route_order() -> None:
    routes = [_route(f"/r{i}") for i in range(5)]
    p = plan(routes)
    assert [rp.route.route for rp in p.routes] == [r.route for r in routes]


def test_plan_accepts_probe_override() -> None:
    p = plan([_route("/x")], probes=["runtime", "network"])
    assert p.routes[0].probes == ["runtime", "network"]


def test_route_plan_probes_are_independent_lists() -> None:
    p = plan([_route("/a"), _route("/b")])
    p.routes[0].probes.append("ui")
    # Mutating one route's probe list must not bleed into another.
    assert p.routes[1].probes == list(DEFAULT_PROBES)


def test_plan_is_sequential_in_v0() -> None:
    assert plan([_route("/x")]).parallel is False
