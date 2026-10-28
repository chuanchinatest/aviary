"""路由解析与三种组模式的单元测试。"""

import pytest

from aviary.routing import Router
from aviary.upstreams import Provider


@pytest.fixture()
def router():
    providers = [
        Provider(name="a", type="openai", base_url="http://a", models=["m1", "m2"]),
        Provider(name="b", type="anthropic", base_url="http://b", models=["c1"]),
    ]
    groups = [
        {"name": "daily", "mode": "order", "members": ["a/m1", "b/c1"]},
        {"name": "rot", "mode": "rotate", "members": ["a/m1", "b/c1"]},
        {"name": "usage", "mode": "usage", "members": ["a/m1", "b/c1"]},
    ]
    return Router(providers, groups, model_map={"fast": "a/m2"}, default_provider="a")


def test_provider_model_and_default(router):
    assert router.resolve("a/m1").candidates == [(router.providers["a"], "m1")]
    assert router.resolve("m1").candidates == [(router.providers["a"], "m1")]
    assert router.resolve("未知名").candidates == [(router.providers["a"], "未知名")]


def test_alias(router):
    assert router.resolve("fast").candidates == [(router.providers["a"], "m2")]


def test_unknown_provider(router):
    with pytest.raises(KeyError):
        router.resolve("nope/m1")
    with pytest.raises(KeyError):
        router.resolve("group/nope")


def test_group_order_keeps_sequence(router):
    assert [c[1] for c in router.resolve("group/daily").candidates] == ["m1", "c1"]


def test_group_rotate(router):
    first = router.resolve("group/rot").candidates[0]
    second = router.resolve("group/rot").candidates[0]
    assert first != second
    assert router.resolve("group/rot").candidates[0] == first  # 周期回到第一个


def test_group_usage_prefers_least_used(router):
    router.record_usage("g", "a/m1")
    router.record_usage("g", "a/m1")
    router.record_usage("g", "b/c1")
    assert router.resolve("group/usage").candidates[0][0].name == "b"
